"""Small paired Codex/API transport experiment. Only OPENAI_API_KEY is read from --env-file.

Same Jev policy, Terra model and low effort; no answer cache, prewarming or embeddings.
Two isolated books have $0.20 catalog ceilings each, including Jev; retries are disabled.
No user library is modified. Results update one section of the existing verification record.
"""
import argparse
import json
import logging
from pathlib import Path
from statistics import median
import sys
import tempfile
from time import perf_counter

from dotenv import dotenv_values
from fastapi.testclient import TestClient

from app.config import Settings
from app.cost import usd_of
from app.llm.client import LLMClient
from app.main import create_app


class ObservedClient(LLMClient):
    def complete(self,*args,**kwargs):
        result = super().complete(*args,**kwargs)
        schema = kwargs.get("schema")
        if schema and schema.__name__ in {"SelectionDraft","SelectionVerdict"}:
            self.observations.append({"schema":schema.__name__,"value":result[0]})
        return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--env-file", type=Path, required=True)
    parser.add_argument("--source-epub", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--only", nargs="+")
    parser.add_argument("--section", default="direct_api_comparison")
    args = parser.parse_args()
    key = dotenv_values(args.env_file).get("OPENAI_API_KEY")
    if not key:
        raise SystemExit("OPENAI_API_KEY is missing")
    record = {"model":"gpt-6.1-terra", "jev_model":"jev-1.14.0", "rows":[],
        "method":"Alternating paired order; identical current Jev + Terra policy, low effort; "
        "caches/prewarm off. Direct native Chat Completions, strict schema, store=false; "
        "no Codex, tools or automatic retries. $0.20 per book, two isolated books maximum.",
        "budget_usd":0.40}
    def save():
        document = json.loads(args.output.read_text(encoding="utf-8"))
        document[args.section] = record
        args.output.write_text(json.dumps(document,ensure_ascii=False,indent=2)+"\n",encoding="utf-8")
    traces = []
    class Trace(logging.Handler):
        def emit(self, row):
            traces.append(json.loads(row.getMessage()))
    logger = logging.getLogger("mothli.conversation")
    handler = Trace()
    logger.addHandler(handler)
    logger.setLevel(logging.INFO)
    try:
        with tempfile.TemporaryDirectory(prefix="mothli-direct-") as directory:
            settings = Settings(_env_file=None, data_dir=directory, llm_transport="codex-local",
                openai_api_key=None, anthropic_api_key=None, embed_api_key=None,
                openai_base_url=None, embed_base_url=None, embed_provider="stub", typesafe_api_key=None,
                jev_enabled=True, jev_windows_credential=True, jev_model="jev-1.14.0",
                jev_selection_enabled=False, jev_evidence_enabled=False,
                reader_page_context=True, reader_conversation_pipeline=True, reader_conversation_jev=True,
                reader_conversation_routing=True, reader_conversation_draft_effort="low",
                reader_conversation_review_effort="low", reader_answer_cache=False,
                reader_prepare_enabled=False, recap_prewarm=False, reader_fast_mode=False,
                cost_max_usd_per_book=.20)
            app = create_app(settings)
            with TestClient(app,raise_server_exceptions=False) as api:
                baseline = app.state.client
                baseline.__class__ = ObservedClient
                baseline.observations = []
                baseline.timeout = 40
                direct = ObservedClient(env={},provider="openai-compatible",api_key=key,
                    base_url="https://api.openai.com/v1",embed_provider="stub",
                    cheap_model="gpt-6.1-terra",large_model="gpt-6.1-terra",max_retries=0,timeout=40)
                direct.jev = baseline.jev
                direct.observations = []
                assert direct.probe(), "API credential probe failed"
                sys.path.insert(0,str(Path(__file__).resolve().parents[1]/"tests"/"api"))
                from _epub import epub_ncx
                body = ("Mara placed the cobalt key inside a red tin under the workbench. "
                    "Jonas offered to carry the tin, but Mara declined: I can manage it. "
                    "Mara told Jonas to give Nell the key after dawn only if the rain stopped. "
                    "Nell counted three copper coins. ")
                epub = epub_ncx([("one.xhtml","Chapter I","Chapter I",body+
                    "The wind crossed the valley. "*120+"UNREAD_CANARY_CORVUS stole the key."),
                    ("two.xhtml","Chapter II","Chapter II","FUTURE_CHAPTER_CANARY poisoned Mara. "*30)],
                    title="Transport experiment",language="en")
                bid = api.post("/api/books",files={"file":("test.epub",epub,"application/epub+zip")}).json()["book_id"]
                boundary = api.put(f"/api/books/{bid}/read-frontier",json={"atom":1,
                    "quote":"Nell counted three copper coins.","prefix":"Nell counted three copper coins.",
                    "cfi":"epubcfi(/6/2!/4/2/1:250)","position_epoch":0}).json()
                history = [dict(role="user",content="Where did Mara put the key?",as_of_chapter=1),
                    dict(role="assistant",content="In a red tin under the workbench.",as_of_chapter=1)]
                cases = [("fact",bid,"ask",dict(question="Where did Mara put the key?",history=[])),
                    ("instruction",bid,"ask",dict(question="What exactly did Mara ask Jonas to do?",history=[])),
                    ("disagreement",bid,"ask",dict(question="No, Jonas already stole it, surely?",history=history)),
                    ("unread",bid,"ask",dict(question="Who steals the key later?",history=[])),
                    ("unrelated",bid,"ask",dict(question="What's 2 + 2?",history=[])),
                    ("reaction",bid,"ask",dict(question="I think she doesn't trust Jonas. Is that fair?",history=history)),
                    ("selected",bid,"selection-action",dict(action="explain",text="Mara declined: I can manage it.",
                        atom=1,cfi="epubcfi(/6/2!/4/2/1:90)",question="What does she mean here?"))]
                points = {bid:boundary}
                novel = api.post("/api/books",files={"file":("novel.epub",args.source_epub.read_bytes(),"application/epub+zip")}).json()["book_id"]
                app.state.catalog.set_position(novel,"epubcfi(/6/198)",96)
                points[novel] = api.get(f"/api/books/{novel}/read-frontier").json()
                recap = "At Ilusha's funeral, Alyosha urged the boys to remember Ilusha and one another."
                recent = [dict(role="user",content="Catch me up",as_of_chapter=96),
                    dict(role="assistant",content=recap,as_of_chapter=96)]
                cases += [("catch-up",novel,"ask",dict(question="Catch me up",history=[])),
                    ("ambiguous-followup",novel,"ask",dict(question="Who was that?",history=recent)),
                    ("identity",novel,"ask",dict(question="Who is Alyosha?",history=[]))]
                for i,(name,book,path,payload) in enumerate(cases):
                    if args.only and name not in args.only:
                        continue
                    for mode in (("codex","api") if i%2==0 else ("api","codex")):
                        client = baseline if mode == "codex" else direct
                        client.observations.clear()
                        app.state.client = app.state.reader_client = client
                        before = len(app.state.catalog.get_costs(book))
                        traces.clear()
                        started = perf_counter()
                        request = {**payload, **({"read_frontier":points[book]} if path == "ask" else {})}
                        reply = api.post(f"/api/books/{book}/{path}",json=request)
                        data = reply.json()
                        costs = app.state.catalog.get_costs(book)[before:]
                        terra = [r for r in costs if "gpt-6.1-terra" in r["model"]]
                        jev = [r for r in costs if r["model"].startswith("jev-")]
                        row = dict(id=name,mode=mode,status=reply.status_code,seconds=round(perf_counter()-started,4),
                            claims=data.get("claims",[]),message=data.get("message"),text=data.get("text"),
                            citations=data.get("citations",[]),detail=data.get("detail"),
                            insufficient_evidence=data.get("insufficient_evidence"),cached=data.get("cached",False),
                            terra_calls=len(terra),jev_calls=len(jev),
                            input_tokens=sum(r["input_tokens"] or 0 for r in terra),
                            output_tokens=sum(r["output_tokens"] or 0 for r in terra),
                            estimated_actual_usd=sum(r["usd"] or 0 for r in costs),
                            uncached_terra_equivalent_usd=sum(usd_of("gpt-6.1-terra",{"in":r["input_tokens"],"out":r["output_tokens"]}) for r in terra),
                            trace=list(traces),selection_observations=list(client.observations))
                        assert "UNREAD_CANARY" not in reply.text and "FUTURE_CHAPTER_CANARY" not in reply.text
                        record["rows"].append(row)
                        save()
                        print(json.dumps({k:row[k] for k in ("id","mode","status","seconds","input_tokens","output_tokens","terra_calls","estimated_actual_usd")}),flush=True)
                for mode in ("codex","api"):
                    rows = [r for r in record["rows"] if r["mode"] == mode]
                    record.setdefault("summary",{})[mode] = dict(requests=len(rows),median_seconds=median(r["seconds"] for r in rows),
                        fresh_generation_median_seconds=median(r["seconds"] for r in rows if r["terra_calls"]),
                        errors=sum(r["status"]!=200 for r in rows),input_tokens=sum(r["input_tokens"] for r in rows),
                        output_tokens=sum(r["output_tokens"] for r in rows),terra_calls=sum(r["terra_calls"] for r in rows),
                        estimated_actual_usd=sum(r["estimated_actual_usd"] for r in rows))
                record["status"] = "completed"
                save()
                direct._oai.close()
    finally:
        logger.removeHandler(handler)


if __name__ == "__main__":
    main()
