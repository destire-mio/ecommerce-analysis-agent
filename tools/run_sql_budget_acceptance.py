"""Deterministic SQL cancellation, retry ownership and approval/resume acceptance."""
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
import fcntl
import json
import os
import sqlite3
import sys
import tempfile
import time
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import agent_loop
import sql_control
from web.server import create_app


class BudgetTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = Path(self.tmp.name)/"test.sqlite"
        c = sqlite3.connect(self.db)
        c.execute("CREATE TABLE nums(n INTEGER)")
        c.executemany("INSERT INTO nums VALUES(?)", [(i,) for i in range(1000)])
        c.commit(); c.close()

    def tearDown(self):
        self.tmp.cleanup()

    def test_cancel_and_reap_before_return(self):
        original = sql_control.subprocess.Popen
        processes = []
        def spawn(*args, **kwargs):
            p = original(*args, **kwargs); processes.append(p); return p
        with patch.object(sql_control.subprocess, "Popen", side_effect=spawn):
            result = sql_control.run_bounded("SELECT COUNT(*) FROM nums a, nums b, nums c", self.db, seconds=.3)
        self.assertEqual(result["error"], "query_timeout")
        self.assertTrue(result["cancelled"])
        self.assertLess(result["elapsed_seconds"], 1.5)
        self.assertTrue(all(p.poll() is not None for p in processes))
        self.assertTrue(result["query_plan"])
        self.assertEqual(sql_control.run_bounded("SELECT SUM(n) FROM nums", self.db)["rows"], [[499500]])

    def test_cancellation_isolated_from_other_query(self):
        with ThreadPoolExecutor(max_workers=2) as pool:
            slow = pool.submit(sql_control.run_bounded, "SELECT COUNT(*) FROM nums a, nums b, nums c", self.db, (), .3)
            fast = pool.submit(sql_control.run_bounded, "SELECT MAX(n) FROM nums", self.db)
            self.assertEqual(fast.result()["rows"], [[999]])
            self.assertEqual(slow.result()["error"], "query_timeout")

    def test_empty_error_and_read_only_are_distinct(self):
        self.assertEqual(sql_control.run_bounded("SELECT n FROM nums WHERE n<0", self.db)["status"], "empty")
        self.assertEqual(sql_control.run_bounded("SELECT bad FROM nums", self.db)["error"], "sql_error")
        self.assertEqual(sql_control.run_bounded("DELETE FROM nums", self.db)["error"], "sql_error")
        self.assertEqual(sql_control.run_bounded("SELECT COUNT(*) FROM nums", self.db)["rows"], [[1000]])

    @staticmethod
    def timeout(seconds):
        return {"error": "query_timeout", "cancelled": True, "timeout_seconds": seconds,
                "elapsed_seconds": seconds, "query_plan": [[0, 0, 0, "SCAN nums"]]}

    def make_pending(self):
        state = {}; control = sql_control.Controller(state, "计算总量")
        for i in range(3):
            result = control.execute(f"SELECT SUM(n*{i+1}) AS total FROM nums", self.db, runner=self.timeout)
        self.assertEqual(result["error"], "sql_approval_required")
        return control, state["pending"]

    def test_rewrites_and_success_do_not_reset_history(self):
        c = sql_control.Controller({}, "退款汇总")
        q = "SELECT SUM(n) FROM nums"
        c.execute(q, self.db, runner=self.timeout)
        self.assertEqual(c.execute(q+";", self.db, runner=lambda _: self.fail("duplicate executed"))["error"], "repeat_timed_out_query")
        c.execute("SELECT COUNT(*) FROM nums", self.db, runner=lambda _: {"status": "ok", "rows": [[1000]]})
        c.execute("SELECT SUM(x.n+1) FROM nums x", self.db, runner=self.timeout)
        result = c.execute("WITH t AS (SELECT n FROM nums) SELECT SUM(n+2) FROM t", self.db, runner=self.timeout)
        self.assertEqual(result["error"], "sql_approval_required")
        self.assertEqual(len(result["approval"]["attempts"]), 3)

    def test_approval_exact_once_and_extension(self):
        c, p = self.make_pending()
        with self.assertRaises(ValueError): c.approve(p["id"], 120)
        c.approve(p["id"], 60)
        result = c.execute(p["sql"], self.db, runner=self.timeout)
        self.assertEqual(result["approval"]["kind"], "extension")
        self.assertEqual(result["approval"]["last_seconds"], 60)
        with self.assertRaises(ValueError): c.approve(p["id"], 60)
        new = result["approval"]
        with self.assertRaises(ValueError): c.approve(new["id"], 60)
        c.approve(new["id"], 120)
        seen = []
        out = c.execute(new["sql"], self.db, runner=lambda seconds: seen.append(seconds) or {"status": "ok", "rows": [[1]]})
        self.assertEqual(out["status"], "ok")
        self.assertEqual(seen, [120])
        self.assertIsNone(c.grant)
        with self.assertRaises(ValueError): c.approve(new["id"], 120)

    def test_data_changes_invalidate_approval(self):
        c, p = self.make_pending()
        db = sqlite3.connect(self.db); db.execute("INSERT INTO nums VALUES(9999)"); db.commit(); db.close()
        with self.assertRaisesRegex(ValueError, "数据源"):
            c.approve(p["id"], 60)

    def test_successful_approved_sql_can_be_reused_with_short_budget(self):
        c, p = self.make_pending()
        c.approve(p["id"], 60)
        c.execute(p["sql"], self.db, runner=lambda _: {"status": "ok"})
        seen = []
        self.assertEqual(c.execute(p["sql"], self.db, runner=lambda s: seen.append(s) or {"status": "ok"})["status"], "ok")
        self.assertEqual(seen, [5])
        self.assertEqual(len(c.state["groups"][0]["attempts"]), 3)

    def test_values_calculation_does_not_inherit_approved_table_timeouts(self):
        c, pending = self.make_pending()
        group = c.state["groups"][0]
        history = deepcopy(group["attempts"])
        c.approve(pending["id"], 60)
        c.execute(pending["sql"], self.db, runner=lambda _: {"status": "ok"})
        seen = []
        query = "WITH amounts(n,price) AS (VALUES (2,1300),(3,1500)) SELECT SUM(n*price) FROM amounts"
        def execute(seconds):
            seen.append(seconds)
            return sql_control.run_bounded(query, self.db, seconds=seconds)
        result = c.execute(query, self.db, runner=execute)
        self.assertEqual(result.get("status"), "ok", result)
        self.assertEqual(result["rows"], [[7100]])
        self.assertEqual(seen, [5])
        self.assertEqual(group["attempts"], history)
        self.assertEqual(group["tables"], ["nums"])
        self.assertEqual(len(c.state["groups"]), 2)
        self.assertFalse(c.state.get("pending"))
        with self.assertRaises(ValueError): c.approve(pending["id"], 60)
        blocked = c.execute("SELECT SUM(n*99) FROM nums", self.db,
                            runner=lambda _: self.fail("table budget was reset"))
        self.assertEqual(blocked["error"], "sql_approval_required")

    def test_table_free_rewrites_keep_own_budget_and_do_not_block_table_queries(self):
        c = sql_control.Controller({}, "计算采购数量")
        for query in ("SELECT 1", "WITH x(n) AS (VALUES (2)) SELECT n FROM x", "SELECT 3"):
            result = c.execute(query, self.db, runner=self.timeout)
        pending = result["approval"]
        self.assertEqual(pending["tables"], [])
        self.assertEqual(len(pending["attempts"]), 3)
        c.approve(pending["id"], 60)
        c.execute(pending["sql"], self.db, runner=lambda _: {"status": "ok"})
        result = c.execute("SELECT SUM(n) FROM nums", self.db)
        self.assertEqual(result["rows"], [[499500]])
        blocked = c.execute("SELECT 4", self.db, runner=lambda _: self.fail("calculation budget was reset"))
        self.assertEqual(blocked["error"], "sql_approval_required")
        self.assertEqual(blocked["approval"]["tables"], [])
        self.assertEqual(len(blocked["approval"]["attempts"]), 3)

    def test_invalid_sql_returns_error_without_consuming_attempt(self):
        c = sql_control.Controller({}, "查询")
        self.assertEqual(c.execute("SELECT FROM (", self.db)["error"], "sql_error")
        self.assertEqual(c.execute("SELECT SUM(t.n) FROM nums t JOIN nums t ON 1=1", self.db)["error"], "sql_error")
        self.assertEqual(c.state["groups"], [])

    def test_same_table_detail_and_count_do_not_inherit_sum_timeouts(self):
        c, pending = self.make_pending()
        c.approve(pending["id"], 60)
        c.execute(pending["sql"], self.db, runner=lambda _: {"status": "ok"})
        history = deepcopy(c.state["groups"][0]["attempts"])
        for sql, expected in [("SELECT n FROM nums ORDER BY n LIMIT 2", [[0], [1]]),
                              ("SELECT COUNT(*) FROM nums", [[1000]])]:
            result = c.execute(sql, self.db)
            self.assertEqual(result.get("rows"), expected, result)
        self.assertEqual(c.state["groups"][0]["attempts"], history)
        self.assertEqual(c.execute("SELECT SUM(n*10) FROM nums", self.db,
                                  runner=lambda _: self.fail("exhausted SUM executed"))["error"],
                         "sql_approval_required")

    def test_different_finance_questions_keep_separate_attempts(self):
        c = sql_control.Controller({}, "财务对账")
        queries = [
            "SELECT component,state,COUNT(*),SUM(amount) FROM finance_entries GROUP BY component,state",
            "SELECT COUNT(*),COUNT(DISTINCT journal_id),COUNT(DISTINCT component) FROM finance_entries",
            "SELECT state,COUNT(*) FROM finance_entries GROUP BY state",
        ]
        for sql in queries:
            result = c.execute(sql, self.db, runner=self.timeout)
            self.assertEqual(result.get("error"), "query_timeout", result)
            self.assertEqual(result["attempts_used"], 1)
        result = c.execute("SELECT state,COUNT(*) FROM finance_entries WHERE shop_id=1 GROUP BY state",
                           self.db, runner=self.timeout)
        self.assertEqual(result["attempts_used"], 2)
        result = c.execute("SELECT f.state,COUNT(*) AS n FROM finance_entries f WHERE shop_id=2 GROUP BY f.state",
                           self.db, runner=self.timeout)
        self.assertEqual(result["error"], "sql_approval_required")
        self.assertEqual(len(result["approval"]["attempts"]), 3)

    def test_renamed_cte_and_subquery_preserve_retry_budget(self):
        c = sql_control.Controller({}, "汇总")
        for sql in ["SELECT SUM(n) FROM nums WHERE n>0",
                    "WITH source AS (SELECT n AS value FROM nums) SELECT SUM(value+1) AS total FROM source",
                    "SELECT SUM(s.value+2) FROM (SELECT n AS value FROM nums) s WHERE s.value>3"]:
            result = c.execute(sql, self.db, runner=self.timeout)
        self.assertEqual(result.get("error"), "sql_approval_required", result)
        self.assertEqual(len(result["approval"]["attempts"]), 3)

    def test_grouping_alias_and_star_cte_preserve_retry_budget(self):
        c = sql_control.Controller({}, "分组计数")
        for sql in ["SELECT n,COUNT(*) FROM nums GROUP BY n",
                    "WITH x AS (SELECT * FROM nums) SELECT n,COUNT(*) FROM x GROUP BY 1",
                    "SELECT t.n AS dimension,COUNT(*) FROM nums t WHERE n>0 GROUP BY dimension"]:
            result = c.execute(sql, self.db, runner=self.timeout)
        self.assertEqual(result.get("error"), "sql_approval_required", result)
        self.assertEqual(len(result["approval"]["attempts"]), 3)

    def test_legacy_checkpoint_reclassifies_without_erasing_timeouts(self):
        c, pending = self.make_pending()
        c.approve(pending["id"], 60)
        c.execute(pending["sql"], self.db, runner=lambda _: {"status": "ok"})
        old = deepcopy(c.state)
        old.pop("grouping_version")
        old["groups"][0].pop("signature")
        old["pending"] = {**pending, "id": "wrong-table-block", "sql": "SELECT n FROM nums LIMIT 2",
                          "fingerprint": sql_control.identity("SELECT n FROM nums LIMIT 2", self.db)}
        restored = sql_control.Controller(old, "汇总")
        self.assertFalse(old.get("pending"))
        self.assertEqual(restored.released_pending["id"], "wrong-table-block")
        result = restored.execute(restored.released_pending["sql"], self.db)
        self.assertEqual(result.get("rows"), [[0], [1]], result)
        self.assertEqual(sum(len(g["attempts"]) for g in old["groups"]), 3)
        # Restoring again cannot discard history or release a second ticket.
        again = sql_control.Controller(json.loads(json.dumps(old)), "汇总")
        self.assertIsNone(again.released_pending)
        result = again.execute("SELECT SUM(n*99) FROM nums", self.db,
                               runner=lambda _: self.fail("history lost during migration"))
        self.assertEqual(result["error"], "sql_approval_required")

    def test_legacy_real_timeout_and_extension_stay_pending(self):
        for extension in (False, True):
            c, pending = self.make_pending()
            if extension:
                c.approve(pending["id"], 60)
                pending = c.execute(pending["sql"], self.db, runner=self.timeout)["approval"]
            state = deepcopy(c.state); state.pop("grouping_version")
            for group in state["groups"]: group.pop("signature")
            restored = sql_control.Controller(state, "汇总")
            self.assertIsNone(restored.released_pending)
            self.assertEqual(state["pending"]["id"], pending["id"])
            restored.approve(pending["id"], 120 if extension else 60)
            result = restored.execute(pending["sql"], self.db, runner=self.timeout)
            self.assertEqual(result["approval"]["kind"], "extension")

    def test_another_query_cannot_use_grant(self):
        c, p = self.make_pending()
        c.approve(p["id"], 60)
        r = c.execute("SELECT SUM(n*99) FROM nums", self.db, runner=lambda _: self.fail("changed SQL executed"))
        self.assertEqual(r["error"], "sql_approval_required")

    def test_params_are_bound_and_sessions_are_isolated(self):
        c = sql_control.Controller({}, "参数查询")
        for value in (1, 2, 3):
            result = c.execute("SELECT SUM(n) FROM nums WHERE n>?", self.db, (value,), runner=self.timeout)
        p = result["approval"]
        self.assertEqual(p["params"], (3,))
        c.approve(p["id"], 60)
        result = c.execute(p["sql"], self.db, (4,), runner=lambda _: self.fail("changed parameters executed"))
        self.assertEqual(result["error"], "sql_approval_required")
        other = sql_control.Controller({}, "另一会话")
        self.assertEqual(other.execute("SELECT SUM(n) FROM nums", self.db)["rows"], [[499500]])

    def test_read_only_cte_supported(self):
        q = "WITH t AS (SELECT n FROM nums) SELECT SUM(n) FROM t"
        self.assertTrue(agent_loop.gate_check(q, str(self.db), "ecommerce", "test").get("ok"))
        self.assertEqual(agent_loop.run_sql(q, str(self.db))["rows"], [[499500]])
        self.assertIn("error", agent_loop.gate_check("WITH t AS (SELECT 1) DELETE FROM nums", str(self.db), "ecommerce", "test"))


class RuntimeTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.oldcwd = os.getcwd(); os.chdir(self.tmp.name)
        p = Path("data/ecommerce"); p.mkdir(parents=True)
        c = sqlite3.connect(p/"ecommerce.sqlite")
        c.execute("CREATE TABLE nums(n INTEGER)"); c.executemany("INSERT INTO nums VALUES(?)", [(1,), (2,), (3,)])
        c.commit(); c.close()

    def tearDown(self):
        os.chdir(self.oldcwd); self.tmp.cleanup()

    def test_resume_obsolete_table_block_executes_at_five_seconds(self):
        db = Path("data/ecommerce/ecommerce.sqlite")
        control = sql_control.Controller({}, "统计后看明细")
        for i in (1, 2, 3):
            pending = control.execute(f"SELECT SUM(n*{i}) FROM nums", db,
                                      runner=BudgetTests.timeout)
        old = deepcopy(control.state)
        old.pop("grouping_version")
        old["groups"][0].pop("signature")
        sql = "SELECT n FROM nums ORDER BY n LIMIT 2"
        old["pending"].update(sql=sql, fingerprint=sql_control.identity(sql, db), id="obsolete")
        state = {"q_count": 0, "executions": {}, "query_control": old,
                 "request_question": "统计后看明细", "request_budget": 12, "used_turns": 10,
                 "pending_tool": {"name": "execute_sql", "args": {"sql": sql}}}
        messages = [{"role": "system", "content": "test"}, {"role": "user", "content": "统计后看明细"},
                    {"role": "assistant", "content": "", "reasoning_content": "reason",
                     "tool_calls": [{"id": "original", "type": "function", "function": {"name": "execute_sql", "arguments": json.dumps({"sql": sql})}}]},
                    {"role": "tool", "tool_call_id": "original", "content": json.dumps(pending)}]
        Path("sessions/recovery").mkdir(parents=True)
        checkpoint = Path("sessions/recovery/runtime.json")
        checkpoint.write_text(json.dumps({"state": state, "messages": messages}))
        seen = []; real = sql_control.run_bounded
        def execute(sql, db, params=(), seconds=5, limit=None):
            seen.append((sql, seconds)); return real(sql, db, params, seconds, limit)
        def create(**kwargs):
            self.assertEqual(kwargs["messages"][-2]["reasoning_content"], "")
            self.assertIn('"rows": [[1], [2]]', kwargs["messages"][-1]["content"])
            msg = SimpleNamespace(content="明细为1和2 (Q1)\n###EVIDENCE###\nQ1: 前两条明细。", tool_calls=None)
            return SimpleNamespace(choices=[SimpleNamespace(message=msg)], usage=None)
        client = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create)))
        with patch.dict(os.environ, {"DEEPSEEK_API_KEY": "test"}), \
             patch.object(agent_loop, "OpenAI", return_value=client), \
             patch.object(sql_control, "run_bounded", side_effect=execute):
            out = agent_loop.run_agent("", "ecommerce", "ecommerce", session_id="recovery", resume=True)
        self.assertEqual(out["status"], "ok", out)
        self.assertEqual(out["turns"], 11)
        self.assertEqual(seen, [(sql, 5)])
        saved = json.loads(checkpoint.read_text())["state"]
        self.assertEqual(sum(len(g["attempts"]) for g in saved["query_control"]["groups"]), 3)
        self.assertNotIn("pending_tool", saved)
        self.assertNotIn("pending", saved["query_control"])

    def test_pause_resume_exact_execution_and_no_extra_work_while_waiting(self):
        class Call:
            def __init__(self, sql):
                self.id = "call-"+str(abs(hash(sql)))
                self.function = SimpleNamespace(name="execute_sql", arguments=json.dumps({"sql": sql, "timeout_seconds": 600}))
            def model_dump(self):
                return {"id": self.id, "type": "function", "function": vars(self.function)}
        class Client:
            def __init__(self): self.calls=0; self.chat=self; self.completions=self
            def create(self, **kwargs):
                for message in kwargs["messages"]:
                    if any(c["id"].startswith("approved-") for c in message.get("tool_calls", [])):
                        if message.get("reasoning_content") != "":
                            raise ValueError("provider rejects approval call without reasoning_content")
                self.calls += 1
                if self.calls <= 3:
                    calls = [Call(f"SELECT SUM(n*{self.calls}) AS total FROM nums")]
                    if self.calls == 3: calls.append(Call("SELECT 999 AS forbidden_extra"))
                    msg = SimpleNamespace(content="", tool_calls=calls, reasoning_content=f"provider-state-{self.calls}")
                else: msg=SimpleNamespace(content="结果为18 (Q1)\n###EVIDENCE###\nQ1: 已批准查询返回18。", tool_calls=None)
                return SimpleNamespace(choices=[SimpleNamespace(message=msg)], usage=None)
        client = Client(); real = sql_control.run_bounded; calls=[]
        def executor(sql, db, params=(), seconds=5, limit=None):
            calls.append((sql, seconds))
            if seconds == 5: return BudgetTests.timeout(seconds)
            return real(sql, db, params, seconds, limit)
        with patch.dict(os.environ, {"DEEPSEEK_API_KEY": "test-not-a-real-key"}), \
             patch.object(agent_loop, "OpenAI", return_value=client), \
             patch.object(sql_control, "run_bounded", side_effect=executor):
            out=agent_loop.run_agent("计算总量", "ecommerce", "ecommerce", session_id="case", budget=8)
            self.assertEqual(out["status"], "awaiting_sql_approval")
            self.assertEqual(len(calls), 3)
            self.assertEqual(client.calls, 3)
            ticket=out["approval"]
            checkpoint=json.loads(Path("sessions/case/runtime.json").read_text())
            saved_assistants=[m for m in checkpoint["messages"] if m["role"]=="assistant"]
            self.assertEqual(saved_assistants[0]["reasoning_content"], "provider-state-1")
            wrong=agent_loop.run_agent("", "ecommerce", "ecommerce", session_id="case", tenant=2,
                approval={"id":ticket["id"],"decision":"approve","seconds":60})
            self.assertEqual(wrong["status"], "error")
            self.assertEqual(len(calls), 3)
            again=agent_loop.run_agent("继续", "ecommerce", "ecommerce", session_id="case")
            self.assertEqual(again["approval"]["id"], ticket["id"])
            self.assertEqual(client.calls, 3)
            out=agent_loop.run_agent("", "ecommerce", "ecommerce", session_id="case",
                approval={"id":ticket["id"],"decision":"approve","seconds":60})
            self.assertEqual(out["status"], "ok", out)
            self.assertEqual(calls[-1], (ticket["sql"],60))
            self.assertEqual(out["executions"]["Q1"]["rows"], [[18]])
            self.assertEqual(out["turns"], 4)
            self.assertEqual(len(calls), 4)
            replay=agent_loop.run_agent("", "ecommerce", "ecommerce", session_id="case",
                approval={"id":ticket["id"],"decision":"approve","seconds":60})
            self.assertEqual(replay["status"], "error")
            self.assertEqual(len(calls), 4)

    def test_model_resume_preserves_result_budget_and_consumed_approval(self):
        Path("sessions/recovery").mkdir(parents=True)
        state={"q_count":1,"executions":{"Q1":{"tool":"execute_sql","rows":[[18]],"payload":{"status":"ok"}}},
               "query_control":{"groups":[]},"request_question":"计算总量","request_budget":4,"used_turns":3}
        messages=[{"role":"system","content":"test"},{"role":"user","content":"计算总量"},
                  {"role":"assistant","content":"按用户审批重跑指定查询。","tool_calls":[{"id":"approved-legacy","type":"function","function":{"name":"execute_sql","arguments":"{}"}}]},
                  {"role":"tool","tool_call_id":"approved-legacy","content":'{"query_ref":"Q1","rows":[[18]]}'}]
        Path("sessions/recovery/runtime.json").write_text(json.dumps({"state":state,"messages":messages}))
        def create(**kwargs):
            history=kwargs["messages"]
            self.assertEqual(history[-2]["reasoning_content"], "")
            self.assertEqual(history[-1]["role"],"tool")
            msg=SimpleNamespace(content="结果为18 (Q1)\n###EVIDENCE###\nQ1: 结果18。",tool_calls=None,reasoning_content="provider-state-final")
            return SimpleNamespace(choices=[SimpleNamespace(message=msg)],usage=None)
        client=SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create)))
        with patch.dict(os.environ,{"DEEPSEEK_API_KEY":"test"}),patch.object(agent_loop,"OpenAI",return_value=client),patch.object(agent_loop,"dispatch",side_effect=AssertionError("SQL must not replay")):
            out=agent_loop.run_agent("", "ecommerce", "ecommerce",session_id="recovery",resume=True)
            self.assertEqual(out["status"],"ok")
            self.assertEqual(out["turns"],4)
            self.assertEqual(out["executions"]["Q1"]["rows"],[[18]])
            rejected=agent_loop.run_agent("", "ecommerce", "ecommerce",session_id="recovery",resume=True)
            self.assertEqual(rejected["status"],"error")

    def test_session_lock_and_path_validation(self):
        self.assertEqual(agent_loop.run_agent("x", "ecommerce", "ecommerce", session_id="../bad")["status"], "error")
        Path("sessions/case").mkdir(parents=True)
        with open("sessions/case/.lock", "a") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            self.assertEqual(agent_loop.run_agent("x", "ecommerce", "ecommerce", session_id="case")["status"], "busy")

    def test_http_approval_separate_from_model_question(self):
        calls=[]
        def runner(*args, **kwargs):
            calls.append(kwargs)
            return {"status":"awaiting_sql_approval","session_id":"case","approval":{"id":"ticket"}}
        client=create_app(runner).test_client()
        r=client.post("/ask",json={"question":"查退款","approval":{"id":"forged","decision":"approve"}})
        self.assertEqual(r.json["approval"]["id"],"ticket")
        self.assertNotIn("approval",calls[-1])
        r=client.post("/query-approval",json={"session_id":"case","id":"ticket","decision":"approve","seconds":60})
        self.assertEqual(calls[-1]["approval"]["seconds"],60)
        self.assertEqual(r.status_code,200)
        self.assertEqual(client.post("/query-approval",json={}).status_code,400)


if __name__ == "__main__":
    unittest.main(verbosity=2)
