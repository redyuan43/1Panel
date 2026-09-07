"""Review regressions with synthetic request content; no inference."""
import copy
import unittest

from ai_router.content_audit import ContentObservation
from ai_router.protocol import move_workbuddy_dynamic_context


class ReorderReviewTests(unittest.TestCase):
    def body(self):
        return {
            "model": "siyuan/qwen36-shared",
            "messages": [
                {"role": "system", "content": "stable instructions"},
                {"role": "user", "name": "synthetic-user", "content": "synthetic question"},
            ],
            "tools": [{"type": "function", "function": {
                "name": name, "description": "shared catalog instructions", "parameters": {"type": "object"},
            }} for name in ["Agent", "Skill"]],
        }

    def inspect(self, before, mutate=None):
        move = move_workbuddy_dynamic_context(before, "chat", client_id="workbuddy-qwen36-shared")
        self.assertTrue(move.moved)
        after = copy.deepcopy(move.body)
        if mutate:
            mutate(after)
        observer = ContentObservation()
        observer.check_workbuddy(before, after, move)
        return {x["check"]: x["status"] for x in observer.checks}

    def test_identical_descriptions_on_distinct_tools_are_preserved_once_each(self):
        checks = self.inspect(self.body())
        self.assertEqual(checks["dynamic_tool_content_once"], "passed")

    def test_user_role_change_is_detected(self):
        checks = self.inspect(self.body(), lambda body: body["messages"][1].update(role="assistant"))
        self.assertEqual(checks["message_order_and_tool_history"], "failed")

    def test_user_metadata_loss_is_detected(self):
        checks = self.inspect(self.body(), lambda body: body["messages"][1].pop("name"))
        self.assertEqual(checks["message_order_and_tool_history"], "failed")

    def test_dynamic_tool_block_duplication_is_detected(self):
        def duplicate(body):
            content = body["messages"][1]["content"]
            start = content.index('<workbuddy_tool_description name="Agent">')
            end = content.index('</workbuddy_tool_description>', start) + len('</workbuddy_tool_description>')
            body["messages"][1]["content"] = content[:end] + "\n" + content[start:end] + content[end:]
        self.assertEqual(self.inspect(self.body(), duplicate)["dynamic_tool_content_once"], "failed")

    def memory_body(self):
        from ai_router.protocol import _WORKBUDDY_MEMORY_HEADING, _WORKBUDDY_MEMORY_END
        body = self.body()
        body["messages"][0]["content"] += "\n" + _WORKBUDDY_MEMORY_HEADING + "\nshared catalog instructions\n" + _WORKBUDDY_MEMORY_END
        return body

    def test_workspace_text_shared_with_tools_is_not_a_duplicate_memory_block(self):
        self.assertEqual(self.inspect(self.memory_body())["workspace_memory_and_stable_content"], "passed")

    def test_optional_null_tools_does_not_break_memory_reorder_audit(self):
        body = self.memory_body()
        body["tools"] = None
        checks = self.inspect(body)
        self.assertTrue(all(status == "passed" for status in checks.values()), checks)

    def test_unexpected_text_in_memory_placeholder_is_detected(self):
        from ai_router.protocol import _WORKBUDDY_MEMORY_PLACEHOLDER
        def corrupt(body):
            body["messages"][0]["content"] = body["messages"][0]["content"].replace(_WORKBUDDY_MEMORY_PLACEHOLDER, "unexpected synthetic instruction")
        self.assertEqual(self.inspect(self.memory_body(), corrupt)["workspace_memory_and_stable_content"], "failed")


if __name__ == "__main__":
    unittest.main()
