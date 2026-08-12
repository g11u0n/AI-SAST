"""Repository-wide AI-SAST runtime with selected evaluation mode."""
from __future__ import annotations
import hashlib, json
from collections import Counter
from pathlib import Path
from typing import Any, Iterable, Sequence
from src.agents import ANALYST_SCHEMA, CONTEXT_SCHEMA, VERIFIER_SCHEMA, AnalystAgent, ContextAgent, VerifierAgent, load_prompt
from src.chunking.batcher import render_evidence_frame
from src.chunking.blob_source import GitBlobSource
from src.chunking.tokenizer import rendered_slice
from src.providers.ollama import OllamaProvider

MAX_EVIDENCE_BYTES=8192

def _read_json(path:Path)->dict[str,Any]:
    value=json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value,dict): raise ValueError(f"Expected JSON object: {path}")
    return value

def _iter_jsonl(path:Path)->Iterable[dict[str,Any]]:
    with path.open("r",encoding="utf-8",newline="\n") as handle:
        for n,line in enumerate(handle,1):
            if not line.strip(): continue
            try: value=json.loads(line)
            except json.JSONDecodeError as exc: raise ValueError(f"Invalid JSONL {path}:{n}") from exc
            if not isinstance(value,dict): raise ValueError(f"Non-object JSONL record {path}:{n}")
            yield value

def _json_text(value:Any)->str:
    return json.dumps(value,ensure_ascii=False,sort_keys=True,separators=(",",":"),allow_nan=False)

def _finding_id(batch_id:str,ordinal:int,title:str)->str:
    basis=f"{batch_id}\0{ordinal}\0{' '.join(title.lower().split())}".encode("utf-8")
    return "F1-"+hashlib.sha256(basis).hexdigest()[:20]

class RuntimeStore:
    def __init__(self,*,root:Path,repository:Path,git_executable:Path)->None:
        self.root=root; self.repository=repository; self.git_executable=git_executable
        self.selection=_read_json(root/"artifacts"/"evaluation"/"selection.json")
        self.selected_batch_ids=list(self.selection["selected_batch_ids"])
        self.batch_records=list(_iter_jsonl(root/"artifacts"/"chunking"/"batch_manifest.jsonl"))
        self.batch_by_id={r["batch_id"]:r for r in self.batch_records}
        missing=set(self.selected_batch_ids)-set(self.batch_by_id)
        if missing: raise ValueError(f"Selected Batch IDs not found: {sorted(missing)}")

        self.priority_manifest_path=root/"artifacts"/"evaluation"/"priority_manifest.jsonl"
        self.priority_batch_ids=[]
        if self.priority_manifest_path.is_file():
            priority_records=list(_iter_jsonl(self.priority_manifest_path))
            priority_records.sort(key=lambda r:(int(r["priority_rank"]),str(r["batch_id"])))
            self.priority_batch_ids=[r["batch_id"] for r in priority_records]
            unknown=set(self.priority_batch_ids)-set(self.batch_by_id)
            if unknown:
                raise ValueError(f"Priority manifest references unknown Batch IDs: {sorted(unknown)[:5]}")
            if len(self.priority_batch_ids)!=len(self.batch_records):
                raise ValueError(
                    "Priority manifest must rank every Batch exactly once: "
                    f"{len(self.priority_batch_ids)} ranked vs {len(self.batch_records)} total"
                )
            if len(set(self.priority_batch_ids))!=len(self.priority_batch_ids):
                raise ValueError("Priority manifest contains duplicate Batch IDs")

        self.file_meta={
            r["path"]:{"raw_size_bytes":r["raw_size_bytes"],"git_blob_oid":r["git_blob_oid"]}
            for r in _iter_jsonl(root/"artifacts"/"chunking"/"file_manifest.jsonl")
        }
        self.chunk_meta={}
        for r in _iter_jsonl(root/"artifacts"/"chunking"/"chunk_manifest.jsonl"):
            self.chunk_meta[r["chunk_id"]]={
                "chunk_id":r["chunk_id"],"path":r["path"],"git_blob_oid":r["git_blob_oid"],
                "source_encoding":r["source_encoding"],"start_byte":r["start_byte"],
                "end_byte_exclusive":r["end_byte_exclusive"],"start_line":r["start_line"],
                "end_line":r["end_line"],"kind":r["kind"],
                "raw_content_sha256":r["raw_content_sha256"],
                "evidence_frame_sha256":r["evidence_frame_sha256"],
                "evidence_frame_utf8_bytes":r["evidence_frame_utf8_bytes"]
            }

        self.symbols=[]; self.symbol_by_id={}
        for r in _iter_jsonl(root/"artifacts"/"index"/"symbol_index.jsonl"):
            c={
                "symbol_id":r["symbol_id"],"name":r["name"],"qualified_name":r["qualified_name"],
                "symbol_kind":r["symbol_kind"],"role":r["role"],"signature":r["signature"],
                "path":r["path"],"anchor_chunk_id":r["reference"]["anchor"]["chunk_id"]
            }
            self.symbols.append(c); self.symbol_by_id[c["symbol_id"]]=c

        self.calls=[]
        for r in _iter_jsonl(root/"artifacts"/"index"/"call_edges.jsonl"):
            self.calls.append({
                "caller_symbol_id":r["caller_symbol_id"],"callee_name":r["callee_name"],
                "candidate_definition_ids":r["candidate_definition_ids"],"resolution":r["resolution"],
                "anchor_chunk_id":r["reference"]["anchor"]["chunk_id"]
            })
        self._blob_source=None; self._blob_cache={}

    def __enter__(self):
        self._blob_source=GitBlobSource(git_executable=self.git_executable,repository=self.repository)
        self._blob_source.__enter__(); return self

    def __exit__(self,exc_type,exc,traceback):
        if self._blob_source is not None: self._blob_source.__exit__(exc_type,exc,traceback)
        self._blob_source=None; self._blob_cache.clear()

    def batch_ids(self,mode:str,limit:int|None=None)->list[str]:
        if mode=="selected":
            ids=list(self.selected_batch_ids)
        elif mode=="all":
            ids=[r["batch_id"] for r in self.batch_records]
        elif mode=="priority":
            if not self.priority_batch_ids:
                raise ValueError(
                    "Priority manifest not found. Run scripts/rank_batches.py first."
                )
            ids=list(self.priority_batch_ids)
        else:
            raise ValueError(f"Unsupported scan mode: {mode}")
        return ids[:limit] if limit is not None else ids

    def _read_blob(self,path:str,oid:str)->bytes:
        if oid in self._blob_cache: return self._blob_cache[oid]
        if self._blob_source is None: raise RuntimeError("RuntimeStore must be used as a context manager")
        fr=self.file_meta[path]
        if fr["git_blob_oid"]!=oid: raise ValueError(f"Chunk/file blob mismatch: {path}")
        raw=self._blob_source.read_blob(oid,expected_size=fr["raw_size_bytes"])
        self._blob_cache[oid]=raw
        return raw

    def render_chunk(self,chunk_id:str)->str:
        c=self.chunk_meta[chunk_id]
        rf=self._read_blob(c["path"],c["git_blob_oid"])
        raw=rf[c["start_byte"]:c["end_byte_exclusive"]]
        if hashlib.sha256(raw).hexdigest()!=c["raw_content_sha256"]:
            raise ValueError(f"Raw Chunk hash mismatch: {chunk_id}")
        content=rendered_slice(raw,c["source_encoding"])
        frame=render_evidence_frame(c,content)
        if hashlib.sha256(frame).hexdigest()!=c["evidence_frame_sha256"]:
            raise ValueError(f"Evidence frame hash mismatch: {chunk_id}")
        if len(frame)!=c["evidence_frame_utf8_bytes"]:
            raise ValueError(f"Evidence frame size mismatch: {chunk_id}")
        return frame.decode("utf-8")

    def render_batch(self,batch_id:str)->tuple[str,list[str]]:
        b=self.batch_by_id[batch_id]
        chunk_ids=list(b["chunk_ids"])
        payload="".join(self.render_chunk(cid) for cid in chunk_ids)
        raw=payload.encode("utf-8")
        if len(raw)!=b["payload_utf8_bytes"]: raise ValueError(f"Batch payload size mismatch: {batch_id}")
        if hashlib.sha256(raw).hexdigest()!=b["payload_sha256"]: raise ValueError(f"Batch payload hash mismatch: {batch_id}")
        if len(raw)>MAX_EVIDENCE_BYTES: raise ValueError(f"Batch exceeds runtime evidence cap: {batch_id}")
        return payload,chunk_ids

    def pack_chunks(self,chunk_ids:Iterable[str],*,max_bytes:int=MAX_EVIDENCE_BYTES)->tuple[str,list[str]]:
        out=[]; used=[]; seen=set(); total=0
        for cid in chunk_ids:
            if cid in seen or cid not in self.chunk_meta: continue
            seen.add(cid); frame=self.render_chunk(cid); width=len(frame.encode("utf-8"))
            if width>max_bytes or (out and total+width>max_bytes): continue
            out.append(frame); used.append(cid); total+=width
        return "".join(out),used

    def candidate_index(self,requested_symbols:Sequence[str],*,batch_chunk_ids:Sequence[str],
                        max_bytes:int=3500)->str:
        batch_set=set(batch_chunk_ids)
        requested=[v.strip() for v in requested_symbols if v.strip()]
        lowered=[v.lower() for v in requested]
        records=[]; direct_ids=set()

        if lowered:
            for s in self.symbols:
                name=s["name"].lower(); qualified=s["qualified_name"].lower()
                if not any(name==w or qualified==w or qualified.endswith("::"+w) for w in lowered):
                    continue
                direct_ids.add(s["symbol_id"])
                if s["anchor_chunk_id"] not in batch_set:
                    records.append({"why":"symbol","name":s["qualified_name"],"kind":s["symbol_kind"],
                                    "path":s["path"],"chunk":s["anchor_chunk_id"],"sid":s["symbol_id"]})

        batch_symbol_ids={s["symbol_id"] for s in self.symbols if s["anchor_chunk_id"] in batch_set}
        for call in self.calls:
            call_in_batch=call["anchor_chunk_id"] in batch_set
            caller_direct=call["caller_symbol_id"] in direct_ids
            if call_in_batch or caller_direct:
                for sid in call["candidate_definition_ids"][:3]:
                    callee=self.symbol_by_id.get(sid)
                    if callee is None or callee["anchor_chunk_id"] in batch_set: continue
                    records.append({"why":"callee","name":callee["qualified_name"],"path":callee["path"],
                                    "chunk":callee["anchor_chunk_id"],"resolution":call["resolution"]})

            if set(call["candidate_definition_ids"]) & (direct_ids|batch_symbol_ids):
                caller=self.symbol_by_id.get(call["caller_symbol_id"])
                if caller is not None and caller["anchor_chunk_id"] not in batch_set:
                    records.append({"why":"caller","name":caller["qualified_name"],"path":caller["path"],
                                    "chunk":caller["anchor_chunk_id"],"resolution":call["resolution"]})

            if lowered and isinstance(call["callee_name"],str) and call["callee_name"].lower() in lowered:
                caller=self.symbol_by_id.get(call["caller_symbol_id"])
                if caller is not None and caller["anchor_chunk_id"] not in batch_set:
                    records.append({"why":"caller","name":caller["qualified_name"],"path":caller["path"],
                                    "chunk":caller["anchor_chunk_id"],"resolution":call["resolution"]})

        deduped=[]; seen=set()
        for r in records:
            key=(str(r.get("why","")),str(r.get("name","")),str(r.get("chunk","")))
            if key in seen: continue
            seen.add(key); deduped.append(r)

        output=[]
        for r in deduped:
            candidate=output+[r]
            if len(_json_text(candidate).encode("utf-8"))>max_bytes: break
            output.append(r)
        return _json_text(output)

class AnalysisRunner:
    def __init__(self,*,root:Path,repository:Path,git_executable:Path)->None:
        self.root=root; self.repository=repository; self.git_executable=git_executable
        report=_read_json(root/"artifacts"/"index"/"phase3_report.json")
        if report.get("status")!="PASS": raise ValueError("Phase 3 Index report is not PASS")
        if report.get("reproducibility",{}).get("status")!="VERIFIED":
            raise ValueError("Phase 3 Index reproducibility is not VERIFIED")
        self.lock=_read_json(root/"experiment.lock.yaml")
        profile=self.lock["semantic"]["runtime_profile"]
        self.options=profile["options"]; self.keep_alive=profile["transport"]["keep_alive"]

    def _agents(self):
        provider=OllamaProvider.from_experiment_lock(self.lock)
        return (
            AnalystAgent(provider=provider,system_prompt=load_prompt(self.root,"analyst"),
                         response_schema=ANALYST_SCHEMA,options=self.options,keep_alive=self.keep_alive,name="analyst"),
            ContextAgent(provider=provider,system_prompt=load_prompt(self.root,"context"),
                         response_schema=CONTEXT_SCHEMA,options=self.options,keep_alive=self.keep_alive,name="context"),
            VerifierAgent(provider=provider,system_prompt=load_prompt(self.root,"verifier"),
                          response_schema=VERIFIER_SCHEMA,options=self.options,keep_alive=self.keep_alive,name="verifier")
        )

    def dry_run(self,*,mode:str="selected",limit:int|None=None)->dict[str,Any]:
        summary=[]
        with RuntimeStore(root=self.root,repository=self.repository,git_executable=self.git_executable) as store:
            ids=store.batch_ids(mode,limit)
            for bid in ids:
                payload,chunks=store.render_batch(bid)
                summary.append({"batch_id":bid,"chunk_count":len(chunks),
                                "payload_utf8_bytes":len(payload.encode("utf-8")),
                                "payload_sha256":hashlib.sha256(payload.encode("utf-8")).hexdigest()})
        return {"status":"PASS","mode":mode,"batch_count":len(summary),"batches":summary}

    def run(self,*,mode:str="selected",limit:int|None=None,output_dir:Path|None=None,
            resume:bool=False)->dict[str,Any]:
        analyst,context,verifier=self._agents()
        output_dir=output_dir or (self.root/"results_v3")
        output_dir.mkdir(parents=True,exist_ok=True)
        batches_dir=output_dir/"batches"; batches_dir.mkdir(parents=True,exist_ok=True)

        all_telemetry=[]; all_findings=[]
        batch_status=Counter(); finding_status=Counter(); truncated_batches=[]
        baseline_bytes=0; proposed_bytes=0
        completed_ids=set()
        resumed_without_telemetry=0
        resumed_without_handoff_metrics=0

        def absorb_batch_result(result:dict[str,Any], *, recovered:bool)->None:
            nonlocal baseline_bytes, proposed_bytes
            nonlocal resumed_without_telemetry, resumed_without_handoff_metrics

            bid=result["batch_id"]
            completed_ids.add(bid)
            analyst_result=result.get("analyst",{})
            workflows=list(result.get("finding_workflows",[]))
            if analyst_result.get("truncated"):
                truncated_batches.append(bid)

            initial_findings=list(analyst_result.get("findings",[]))
            if not initial_findings:
                batch_status["CLEAR"]+=1
            else:
                if any(w.get("final_status")=="VERIFIED" for w in workflows):
                    batch_status["HAS_VERIFIED_FINDING"]+=1
                elif any(w.get("final_status")=="INCONCLUSIVE" for w in workflows):
                    batch_status["INCONCLUSIVE"]+=1
                else:
                    batch_status["NO_VERIFIED_FINDING"]+=1

            for workflow in workflows:
                initial=workflow.get("initial") or {}
                refined=workflow.get("refined")
                current=refined if isinstance(refined,dict) else initial
                status=workflow.get("final_status","INCONCLUSIVE")
                finding_status[status]+=1
                all_findings.append({
                    "finding_id":workflow.get("finding_id",""),
                    "batch_id":bid,
                    "batch_ordinal":result.get("batch_ordinal"),
                    "title":current.get("title",initial.get("title","")),
                    "reason":current.get("reason",initial.get("reason","")),
                    "evidence_chunk_ids":current.get("chunk_ids",initial.get("chunk_ids",[])),
                    "final_status":status
                })

            if recovered:
                telemetry=result.get("telemetry")
                if isinstance(telemetry,list):
                    all_telemetry.extend(telemetry)
                else:
                    resumed_without_telemetry+=1

                handoff=result.get("handoff_efficiency")
                if isinstance(handoff,dict):
                    baseline_bytes+=int(handoff.get("baseline_retransmission_utf8_bytes",0))
                    proposed_bytes+=int(handoff.get("proposed_handoff_utf8_bytes",0))
                else:
                    resumed_without_handoff_metrics+=1

        def make_summary(status:str,total_target:int)->dict[str,Any]:
            avoided=baseline_bytes-proposed_bytes
            reduction=round(100.0*avoided/baseline_bytes,2) if baseline_bytes else 0.0
            agent_calls=Counter(i["agent"] for i in all_telemetry)
            purpose_calls=Counter(i["purpose"] for i in all_telemetry)
            completed=len(completed_ids)
            return {
                "schema_version":4,
                "status":status,
                "scan_mode":mode,
                "batch_target_count":total_target,
                "batch_count":completed,
                "remaining_batch_count":max(0,total_target-completed),
                "batch_status_counts":dict(sorted(batch_status.items())),
                "finding_count":len(all_findings),
                "finding_status_counts":dict(sorted(finding_status.items())),
                "agent_call_counts":dict(sorted(agent_calls.items())),
                "purpose_call_counts":dict(sorted(purpose_calls.items())),
                "truncated_batch_count":len(truncated_batches),
                "truncation_semantics":"analyst_output_enumeration_only; batch evidence is never silently truncated",
                "truncated_batch_ids":truncated_batches,
                "total_prompt_eval_count":sum(i["prompt_eval_count"] for i in all_telemetry),
                "total_eval_count":sum(i["eval_count"] for i in all_telemetry),
                "resume":{
                    "enabled":resume,
                    "recovered_batch_count":sum(
                        1 for p in batches_dir.glob("batch_*.json")
                        if p.is_file()
                    ) if resume else 0,
                    "batches_without_recoverable_telemetry":resumed_without_telemetry,
                    "batches_without_recoverable_handoff_metrics":resumed_without_handoff_metrics
                },
                "handoff_efficiency":{
                    "measurement":"utf8_bytes_of_cross_agent_evidence_payloads",
                    "baseline_policy":"repeat_original_raw_batch_on_each_handoff",
                    "proposed_policy":"reference_only_index_then_minimal_evidence_pull",
                    "baseline_retransmission_utf8_bytes":baseline_bytes,
                    "proposed_handoff_utf8_bytes":proposed_bytes,
                    "avoided_utf8_bytes":avoided,
                    "reduction_percent":reduction,
                    "complete":resumed_without_handoff_metrics==0,
                    "note":"If old v3 Batch files were resumed, pre-upgrade handoff/token telemetry cannot be reconstructed."
                },
                "telemetry_complete":resumed_without_telemetry==0
            }

        def write_rollups(summary:dict[str,Any])->None:
            with (output_dir/"telemetry.jsonl").open("w",encoding="utf-8",newline="\n") as h:
                for item in all_telemetry:
                    h.write(_json_text(item)+"\n")
            with (output_dir/"findings.jsonl").open("w",encoding="utf-8",newline="\n") as h:
                for item in all_findings:
                    h.write(_json_text(item)+"\n")
            (output_dir/"summary.json").write_text(
                json.dumps(summary,ensure_ascii=False,indent=2,sort_keys=True)+"\n",
                encoding="utf-8"
            )

        with RuntimeStore(root=self.root,repository=self.repository,git_executable=self.git_executable) as store:
            batch_ids=store.batch_ids(mode,limit); total=len(batch_ids)

            if resume:
                for b_ord,bid in enumerate(batch_ids,1):
                    p=batches_dir/f"batch_{b_ord:04d}.json"
                    if not p.is_file():
                        continue
                    try:
                        existing=_read_json(p)
                    except Exception:
                        continue
                    if existing.get("batch_id")!=bid:
                        continue
                    absorb_batch_result(existing,recovered=True)
                if completed_ids:
                    print(f"[resume] recovered {len(completed_ids)}/{total} completed Batches",flush=True)

            try:
                for b_ord,bid in enumerate(batch_ids,1):
                    if bid in completed_ids:
                        continue

                    batch_evidence,batch_chunks=store.render_batch(bid)
                    batch_bytes=len(batch_evidence.encode("utf-8"))
                    batch_telemetry=[]
                    batch_baseline=0; batch_proposed=0

                    print(f"[{b_ord}/{total}] Analyst Batch scan: {bid}",flush=True)
                    first=analyst.analyze_batch(batch_id=bid,evidence=batch_evidence,evidence_chunk_ids=batch_chunks)
                    item={"batch_id":bid,"batch_ordinal":b_ord,**first.telemetry()}
                    all_telemetry.append(item); batch_telemetry.append(item)

                    initial_findings=list(first.result["findings"])
                    workflows=[]

                    for f_ord,initial in enumerate(initial_findings,1):
                        fid=_finding_id(bid,f_ord,initial["title"])
                        workflow={"finding_id":fid,"initial":initial,"context":None,"refined":None,
                                  "verifier":None,"final_status":"INCONCLUSIVE"}
                        current=dict(initial); pulled=[]

                        if current["status"]=="NEED_CONTEXT":
                            candidate_text=store.candidate_index(
                                current["context_symbols"],
                                batch_chunk_ids=batch_chunks,
                                max_bytes=2800
                            )
                            candidate_items=json.loads(candidate_text)
                            print(f"  - {fid}: Context navigation {current['context_symbols']}",flush=True)

                            if not candidate_items:
                                workflow["context"]={
                                    "status":"NONE",
                                    "chunk_ids":[],
                                    "reason":"No repository-index candidate matched the requested project context."
                                }
                                print(f"  - {fid}: Context index miss; no LLM context call",flush=True)
                            else:
                                cc=context.select(
                                    batch_id=bid,
                                    finding_id=fid,
                                    finding_title=current["title"],
                                    finding_reason=current["reason"],
                                    requested_symbols=current["context_symbols"],
                                    candidate_index_text=candidate_text
                                )
                                workflow["context"]=cc.result
                                item={"batch_id":bid,"batch_ordinal":b_ord,"finding_id":fid,**cc.telemetry()}
                                all_telemetry.append(item); batch_telemetry.append(item)
                                baseline_bytes+=batch_bytes; proposed_bytes+=cc.evidence_utf8_bytes
                                batch_baseline+=batch_bytes; batch_proposed+=cc.evidence_utf8_bytes

                                if cc.result["status"]=="SELECTED":
                                    original=[cid for cid in current["chunk_ids"] if cid in batch_chunks]
                                    refine_evidence,pulled=store.pack_chunks(original+cc.result["chunk_ids"])
                                    if refine_evidence:
                                        print(f"  - {fid}: Analyst refinement on {len(pulled)} referenced chunks",flush=True)
                                        rr=analyst.refine_finding(batch_id=bid,finding_id=fid,title=current["title"],
                                                                  evidence=refine_evidence,evidence_chunk_ids=pulled)
                                        workflow["refined"]=rr.result; current=dict(rr.result)
                                        item={"batch_id":bid,"batch_ordinal":b_ord,"finding_id":fid,**rr.telemetry()}
                                        all_telemetry.append(item); batch_telemetry.append(item)
                                        baseline_bytes+=batch_bytes; proposed_bytes+=rr.evidence_utf8_bytes
                                        batch_baseline+=batch_bytes; batch_proposed+=rr.evidence_utf8_bytes

                        if current["status"]=="CLEAR":
                            workflow["final_status"]="REJECTED"
                        else:
                            # Every security-relevant finding is independently adjudicated.
                            # Prefer refined/context evidence, but never suppress verification
                            # merely because context lookup returned NONE.
                            preferred=[]
                            for cid in list(current.get("chunk_ids",[]))+list(pulled)+list(initial.get("chunk_ids",[])):
                                if cid in store.chunk_meta and cid not in preferred:
                                    preferred.append(cid)
                            verify_evidence,used=store.pack_chunks(preferred)
                            if not verify_evidence:
                                # Last-resort evidence is the complete original Batch.
                                verify_evidence=batch_evidence
                                used=list(batch_chunks)
                            print(f"  - {fid}: Independent verification",flush=True)
                            vc=verifier.verify(batch_id=bid,finding_id=fid,title=current["title"],
                                               reason=current["reason"],
                                               evidence=verify_evidence,evidence_chunk_ids=used)
                            workflow["verifier"]=vc.result
                            workflow["final_status"]=vc.result["status"]
                            item={"batch_id":bid,"batch_ordinal":b_ord,"finding_id":fid,**vc.telemetry()}
                            all_telemetry.append(item); batch_telemetry.append(item)
                            baseline_bytes+=batch_bytes; proposed_bytes+=vc.evidence_utf8_bytes
                            batch_baseline+=batch_bytes; batch_proposed+=vc.evidence_utf8_bytes

                        workflows.append(workflow)

                    batch_result={
                        "schema_version":4,
                        "batch_id":bid,
                        "batch_ordinal":b_ord,
                        "raw_batch_utf8_bytes":batch_bytes,
                        "raw_batch_chunk_ids":batch_chunks,
                        "analyst":first.result,
                        "finding_workflows":workflows,
                        "telemetry":batch_telemetry,
                        "handoff_efficiency":{
                            "baseline_retransmission_utf8_bytes":batch_baseline,
                            "proposed_handoff_utf8_bytes":batch_proposed
                        }
                    }

                    # Atomic per-Batch checkpoint: a completed file is safe to skip on resume.
                    final_path=batches_dir/f"batch_{b_ord:04d}.json"
                    temp_path=final_path.with_suffix(".json.tmp")
                    temp_path.write_text(
                        json.dumps(batch_result,ensure_ascii=False,indent=2,sort_keys=True)+"\n",
                        encoding="utf-8"
                    )
                    temp_path.replace(final_path)

                    absorb_batch_result(batch_result,recovered=False)

                    checkpoint=make_summary("RUNNING",total)
                    (output_dir/"checkpoint.json").write_text(
                        json.dumps(checkpoint,ensure_ascii=False,indent=2,sort_keys=True)+"\n",
                        encoding="utf-8"
                    )

            except KeyboardInterrupt:
                summary=make_summary("INTERRUPTED",total)
                write_rollups(summary)
                print(
                    f"\n[interrupt] saved {summary['batch_count']}/{total} completed Batches. "
                    f"Resume with the same command plus --resume.",
                    flush=True
                )
                return summary

        summary=make_summary("PASS",total)
        write_rollups(summary)
        return summary

SelectedBatchRunner=AnalysisRunner
