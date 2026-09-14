import importlib.util
from pathlib import Path
import json

spec = importlib.util.spec_from_file_location("context_quality",
    Path(__file__).resolve().parents[1] / "scripts/context_quality.py")
quality = importlib.util.module_from_spec(spec)
spec.loader.exec_module(quality)


def questions():
    return [{"service": f"ORBIT-{n:03d}", "question": "最终端口？", "answer": str(17000 + n)} for n in range(40)]


def answers():
    return {"facts": [{"service": x["service"], "port": x["answer"]} for x in questions()]}


def test_blind_question_does_not_send_expected_answers():
    request = quality.question_messages([], questions())
    payload = json.loads(request[1]["content"])
    assert all(set(item) == {"service", "question"} for item in payload["questions"])
    assert "17000" not in json.dumps(request)


def test_threshold_and_later_round_correction():
    result = answers()
    assert quality.score_answers(result, questions())["correct"] == 40
    result["facts"][0]["port"] = "28005"
    assert quality.score_answers(result, questions(), 5)["correct"] == 40
    result["facts"] = result["facts"][:-2]
    assert quality.score_answers(result, questions(), 5)["passed"]
    result["facts"].pop()
    assert not quality.score_answers(result, questions(), 5)["passed"]


def test_duplicate_or_extra_answers_do_not_pass_even_if_all_ports_appear():
    result = answers()
    result["facts"].append(result["facts"][0])
    assert not quality.score_answers(result, questions())["passed"]
    result = answers()
    result["facts"].append({"service": "UNKNOWN", "port": "123"})
    assert not quality.score_answers(result, questions())["passed"]
