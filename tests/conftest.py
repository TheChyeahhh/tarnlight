import pytest
from tarnlight import demo


@pytest.fixture
def sample_records():
    """600 made-up calls in the real wire shape (tarnlight.demo): three senders, eight questions, "policy" the busiest,
    18 failed calls. The same every run."""
    return demo.records()


@pytest.fixture
def make_record():
    """A canonical record in the real wire shape: one noul, one choice and one score."""
    def make(i, state=None):
        return {"v": 1, "ts": 1790400000.0 + i * 0.1, "seq": None, "source": "proxy", "session_id": None, "tool_use_id": None,
                "project": "test", "label": None, "sdk": "python/0.7.1", "request_id": f"req_{i:08x}", "latency_ms": 100 + i % 50,
                "status": 200, "retry_count": 0, "cost_est_micro": 15,
                "request": {"state": state if state is not None else {"tick": i, "queue": {"billing": {"wait_s": round(100 + i * 0.01, 2)}}},
                            "model": "jev-latest",
                            "questions": {"go": {"type": "noul", "instructions": "Go?"},
                                          "verdict": {"type": "choice", "instructions": "Approve, reject or defer?",
                                                   "criteria": {"approve": None, "reject": None, "defer": None}},
                                          "size": {"type": "score", "instructions": "How big?", "criteria": ["small", "medium", "large"]}}},
                "response": {"model": "jev-1.13.0",
                             "answers": {"go": {"type": "noul", "noul": (i % 100) / 100},
                                         "verdict": {"type": "choice", "choice": "approve", "confidence": 0.7,
                                                  "probabilities": {"reject": 0.1, "approve": 0.8, "defer": 0.1}},
                                         "size": {"type": "score", "score": 1.2, "confidence": 0.5,
                                                  "legend": {"0": "small", "1": "medium", "2": "large"},
                                                  "probabilities": {"0": 0.1, "1": 0.6, "2": 0.3}}},
                             "usage": {"input_tokens": 300, "output_tokens": 60}},
                "error": None}
    return make
