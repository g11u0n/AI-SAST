"""Role-specialized LLM agents for AI-SAST."""
from __future__ import annotations
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping
from src.providers.base import LLMProvider

ANALYST_SCHEMA: dict[str, Any] = {
    "type":"object",
    "properties":{
        "findings":{"type":"array","maxItems":8,"items":{
            "type":"object",
            "properties":{
                "status":{"type":"string","enum":["CANDIDATE","NEED_CONTEXT"]},
                "title":{"type":"string","maxLength":100},
                "reason":{"type":"string","maxLength":220},
                "chunk_ids":{"type":"array","items":{"type":"string","maxLength":27},"maxItems":3},
                "context_symbols":{"type":"array","items":{"type":"string","maxLength":80},"maxItems":3}
            },
            "required":["status","title","reason","chunk_ids","context_symbols"],
            "additionalProperties":False
        }},
        "truncated":{"type":"boolean"},
        "review":{"type":"array","items":{"type":"string","maxLength":120},"minItems":5,"maxItems":5}
    },
    "required":["findings","truncated","review"],
    "additionalProperties":False
}

REFINEMENT_SCHEMA: dict[str, Any] = {
    "type":"object",
    "properties":{
        "status":{"type":"string","enum":["CANDIDATE","CLEAR","NEED_CONTEXT"]},
        "title":{"type":"string","maxLength":100},
        "reason":{"type":"string","maxLength":220},
        "chunk_ids":{"type":"array","items":{"type":"string","maxLength":27},"maxItems":4},
        "context_symbols":{"type":"array","items":{"type":"string","maxLength":80},"maxItems":3}
    },
    "required":["status","title","reason","chunk_ids","context_symbols"],
    "additionalProperties":False
}

CONTEXT_SCHEMA: dict[str, Any] = {
    "type":"object",
    "properties":{
        "status":{"type":"string","enum":["SELECTED","NONE"]},
        "chunk_ids":{"type":"array","items":{"type":"string","maxLength":27},"maxItems":2},
        "reason":{"type":"string","maxLength":180}
    },
    "required":["status","chunk_ids","reason"],
    "additionalProperties":False
}

VERIFIER_SCHEMA: dict[str, Any] = {
    "type":"object",
    "properties":{
        "status":{"type":"string","enum":["VERIFIED","REJECTED","INCONCLUSIVE"]},
        "reason":{"type":"string","maxLength":240},
        "chunk_ids":{"type":"array","items":{"type":"string","maxLength":27},"maxItems":4}
    },
    "required":["status","reason","chunk_ids"],
    "additionalProperties":False
}

@dataclass
class AgentCall:
    agent: str
    result: dict[str, Any]
    prompt_eval_count: int
    eval_count: int
    evidence_utf8_bytes: int
    evidence_chunk_ids: list[str]
    purpose: str
    def telemetry(self)->dict[str,Any]:
        return {
            "agent":self.agent,"purpose":self.purpose,
            "prompt_eval_count":self.prompt_eval_count,
            "eval_count":self.eval_count,
            "evidence_utf8_bytes":self.evidence_utf8_bytes,
            "evidence_chunk_ids":self.evidence_chunk_ids
        }

class _AgentBase:
    def __init__(self,*,provider:LLMProvider,system_prompt:str,response_schema:Mapping[str,Any],
                 options:Mapping[str,Any],keep_alive:str,name:str)->None:
        self.provider=provider
        self.system_prompt=system_prompt.strip()
        self.response_schema=dict(response_schema)
        self.options=dict(options)
        self.keep_alive=keep_alive
        self.name=name

    def _run(self,*,user_instruction:str,evidence:str,structured_state:str,purpose:str,
             evidence_chunk_ids:list[str],response_schema:Mapping[str,Any]|None=None)->AgentCall:
        schema=dict(response_schema or self.response_schema)
        if len(self.system_prompt.encode("utf-8"))>768: raise ValueError(f"{self.name} system prompt exceeds locked 768-byte cap")
        if len(user_instruction.encode("utf-8"))>256: raise ValueError(f"{self.name} user instruction exceeds locked 256-byte cap")
        if len(evidence.encode("utf-8"))>8192: raise ValueError(f"{self.name} evidence exceeds locked 8192-byte cap")
        if len(structured_state.encode("utf-8"))>256: raise ValueError(f"{self.name} state exceeds locked 256-byte cap")
        user_message=user_instruction+"\n"+evidence+"\n"+structured_state
        components={
            "system_instruction":self.system_prompt,
            "user_instruction":user_instruction,
            "evidence_or_raw_batch":evidence,
            "structured_state":structured_state
        }
        raw=self.provider.chat(
            messages=[{"role":"system","content":self.system_prompt},{"role":"user","content":user_message}],
            response_schema=schema,options=self.options,stream=False,keep_alive=self.keep_alive,
            tools=None,component_payloads=components
        )
        parsed=json.loads(raw["message"]["content"])
        return AgentCall(
            agent=self.name,result=parsed,
            prompt_eval_count=int(raw["prompt_eval_count"]),
            eval_count=int(raw["eval_count"]),
            evidence_utf8_bytes=len(evidence.encode("utf-8")),
            evidence_chunk_ids=list(evidence_chunk_ids),
            purpose=purpose
        )

class AnalystAgent(_AgentBase):
    def analyze_batch(self,*,batch_id:str,evidence:str,evidence_chunk_ids:list[str])->AgentCall:
        return self._run(
            user_instruction="Audit every chunk. review has 5 items: memory/size, input validation, lifetime/resource, API/state, CLEAR rationale. Empty findings require concrete checks; unresolved security flow must be NEED_CONTEXT.",
            evidence=evidence,
            structured_state=json.dumps({"batch":batch_id,"round":0},separators=(",",":")),
            purpose="first_pass",evidence_chunk_ids=evidence_chunk_ids
        )

    def refine_finding(self,*,batch_id:str,finding_id:str,title:str,evidence:str,
                       evidence_chunk_ids:list[str])->AgentCall:
        safe_title=" ".join(title.split())[:100]
        return self._run(
            user_instruction="Reassess this one finding with pulled evidence; return CANDIDATE, CLEAR, or NEED_CONTEXT.",
            evidence=evidence,
            structured_state=json.dumps(
                {"batch":batch_id,"finding":finding_id,"title":safe_title},
                separators=(",",":")
            ),
            purpose="refinement",evidence_chunk_ids=evidence_chunk_ids,
            response_schema=REFINEMENT_SCHEMA
        )

class ContextAgent(_AgentBase):
    def select(self,*,batch_id:str,finding_id:str,finding_title:str,finding_reason:str,
               requested_symbols:list[str],candidate_index_text:str)->AgentCall:
        candidates=json.loads(candidate_index_text)
        finding={
            "title":" ".join(finding_title.split())[:90],
            "reason":" ".join(finding_reason.split())[:150],
            "requested_symbols":requested_symbols[:3]
        }
        evidence=json.dumps({"finding":finding,"candidates":candidates},
                            ensure_ascii=False,separators=(",",":"))
        while candidates and len(evidence.encode("utf-8"))>8192:
            candidates=candidates[:-1]
            evidence=json.dumps({"finding":finding,"candidates":candidates},
                                ensure_ascii=False,separators=(",",":"))
        return self._run(
            user_instruction="Select up to two repository chunks that best resolve this concrete finding.",
            evidence=evidence,
            structured_state=json.dumps({"batch":batch_id,"finding":finding_id},separators=(",",":")),
            purpose="context_selection",evidence_chunk_ids=[]
        )

class VerifierAgent(_AgentBase):
    def verify(self,*,batch_id:str,finding_id:str,title:str,reason:str,evidence:str,
               evidence_chunk_ids:list[str])->AgentCall:
        safe_title=" ".join(title.split())[:90]
        safe_reason=" ".join(reason.split())[:110]
        instruction=f"Verify or disprove: {safe_title}. Basis: {safe_reason}"
        if len(instruction.encode("utf-8"))>256:
            instruction="Independently verify or disprove this security finding from the supplied code."
        return self._run(
            user_instruction=instruction,evidence=evidence,
            structured_state=json.dumps({"batch":batch_id,"finding":finding_id},separators=(",",":")),
            purpose="verification",evidence_chunk_ids=evidence_chunk_ids
        )

def load_prompt(root:Path,name:str)->str:
    return (root/"prompts"/"runtime"/f"{name}.md").read_text(encoding="utf-8").strip()
