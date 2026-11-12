import json

import anthropic
import httpx2
import pytest
from sqlmodel import Session, select

from radar.config import LlmConfig
from radar.llm.provider import AnthropicProvider, LLMError, LLMUnavailable
from radar.models import Listing, Property, PropertyFeature
from radar.normalize.extract import apply_llm, apply_rules, description_hash, resolved_features
from radar.normalize.schema import ExtractedFeatures, verify

DESC = "Appartement au 3ème étage avec ascenseur. Vue mer. Syndic 500 DH/mois."
EMPTY = {"value": None, "confidence": 0, "source_span": None}


def payload(**overrides) -> dict:
    d = {k: dict(EMPTY) for k in ExtractedFeatures.model_fields}
    d.update(overrides)
    return d


class FakeProvider:
    name = "llm:fake"

    def __init__(self, data=None, exc=None):
        self.data, self.exc, self.calls = data, exc, 0

    def extract(self, schema, system, text):
        self.calls += 1
        if self.exc:
            raise self.exc
        return schema.model_validate(self.data)


def test_verify_rejects_fabricated_span():
    feats = ExtractedFeatures.model_validate(payload(
        elevator={"value": True, "confidence": 0.95, "source_span": "avec ascenseur"},
        parking={"value": True, "confidence": 0.9, "source_span": "place de parking"},   # not in text
        terrace_m2={"value": 10, "confidence": 0.8, "source_span": None},              # no evidence
        floor={"value": 3, "confidence": 1.7, "source_span": "3ÈME ÉTAGE"},             # case/accents ok
    ))
    feats, rejected = verify(feats, DESC)
    assert sorted(rejected) == ["parking", "terrace_m2"]
    assert feats.parking.value is None and feats.parking.confidence == 0
    assert feats.elevator.value is True
    assert feats.floor.value == 3 and feats.floor.confidence == 1.0   # clamped


@pytest.fixture
def listing(engine, clock):
    with Session(engine) as db:
        p = Property(zone="anfa", first_seen=clock(), last_seen=clock())
        db.add(p)
        db.flush()
        l = Listing(property_id=p.id, source="mubawab", external_id="1", url="u", description=DESC,
                    description_hash=description_hash(DESC), first_seen=clock(), last_seen=clock())
        db.add(l)
        db.commit()
        return l.id


def test_rules_and_llm_stored_and_resolved(engine, clock, listing):
    prov = FakeProvider(payload(
        elevator={"value": True, "confidence": 0.95, "source_span": "avec ascenseur"},
        sea_view={"value": "full", "confidence": 0.9, "source_span": "Vue mer"},
        parking={"value": True, "confidence": 0.9, "source_span": "garage"},  # hallucinated
    ))
    with Session(engine) as db:
        l = db.get(Listing, listing)
        apply_rules(db, l, clock())
        rejected = apply_llm(db, l, prov, clock())
        db.commit()
        assert rejected == ["parking"]
        assert l.extracted_hash == l.description_hash
        rows = db.exec(select(PropertyFeature)).all()
        assert {r.extracted_by for r in rows} == {"rules", "llm:fake"}
        best = resolved_features(db, l.property_id, 0.5)
        assert best["elevator"].extracted_by == "llm:fake"         # higher confidence wins
        assert best["syndic_fees_mad"].value == 500                 # rules-only fact kept
        assert "parking" not in best                                # never fabricated
        # idempotent: re-running doesn't duplicate rows
        apply_rules(db, l, clock())
        db.commit()
        assert len(db.exec(select(PropertyFeature)).all()) == len(rows)


def _client(handler):
    return anthropic.Anthropic(api_key="test", max_retries=0,
                               http_client=anthropic.DefaultHttpxClient(transport=httpx2.MockTransport(handler)))


CFG = LlmConfig(enabled=True, provider="anthropic", model="claude-opus-5-5", effort="low",
                max_tokens=4000, max_extractions_per_run=10, min_confidence=0.5)


def _msg(text, stop="end_turn"):
    return {"id": "msg_1", "type": "message", "role": "assistant", "model": "claude-opus-5-5",
            "content": [{"type": "text", "text": text}] if text else [], "stop_reason": stop, "stop_sequence": None,
            "usage": {"input_tokens": 1, "output_tokens": 1}}


def test_anthropic_provider_request_shape():
    seen = {}

    def handler(req):
        seen["body"], seen["beta"] = json.loads(req.content), req.headers.get("anthropic-beta")
        return httpx2.Response(200, json=_msg(json.dumps(payload(
            elevator={"value": True, "confidence": 0.9, "source_span": "avec ascenseur"}))))

    out = AnthropicProvider(CFG, client=_client(handler)).extract(ExtractedFeatures, "sys", DESC)
    assert out.elevator.value is True
    b = seen["body"]
    assert b["model"] == "claude-opus-5-5" and b["fallbacks"] == "default"
    assert b["output_config"]["effort"] == "low" and b["output_config"]["format"]["type"] == "json_schema"
    assert seen["beta"] == "server-side-fallback-2026-07-01"
    assert "thinking" not in b and "temperature" not in b


def test_anthropic_provider_errors():
    with pytest.raises(LLMUnavailable):
        AnthropicProvider(CFG, client=_client(lambda r: httpx2.Response(
            401, json={"type": "error", "error": {"type": "authentication_error", "message": "bad key"}}))
        ).extract(ExtractedFeatures, "s", "t")
    with pytest.raises(LLMError, match="refused"):
        AnthropicProvider(CFG, client=_client(lambda r: httpx2.Response(200, json=_msg("", "refusal")))
                          ).extract(ExtractedFeatures, "s", "t")
    with pytest.raises(LLMError, match="unparseable"):
        AnthropicProvider(CFG, client=_client(lambda r: httpx2.Response(200, json=_msg('{"floor": 1}')))
                          ).extract(ExtractedFeatures, "s", "t")
