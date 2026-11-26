"""Bounded, owner-scoped jobs for long HTTP requests. No disk persistence."""
import asyncio
import logging
import time
import uuid

LOG = logging.getLogger("study.jobs")


class JobStore:
    def __init__(self, max_active=2, ttl=900, max_records=64):
        self.items = {}
        self.max_active = max_active
        self.ttl = ttl
        self.max_records = max_records

    def prune(self):
        now = time.monotonic()
        for key, job in list(self.items.items()):
            if job["state"] != "running" and now - job["finished"] > self.ttl:
                self.items.pop(key, None)

    def start(self, owner, kind, work):
        self.prune()
        active = [j for j in self.items.values() if j["state"] == "running"]
        if any(j["owner"] == owner for j in active):
            return {"error": "Предыдущий запрос этого профиля ещё выполняется."}
        if len(active) >= self.max_active or len(self.items) >= self.max_records:
            return {"error": "Сервер занят. Повтори запрос немного позже."}
        key = uuid.uuid4().hex
        job = {"owner": owner, "kind": kind, "state": "running", "started": time.monotonic()}
        self.items[key] = job

        async def run():
            LOG.info("Web job %s %s started", key[:8], kind)
            try:
                result = await work()
                if not isinstance(result, dict):
                    raise RuntimeError("Unexpected job response")
                job["result"] = result
                job["state"] = "error" if result.get("error") else "done"
            except asyncio.CancelledError:
                job["state"] = "error"
                job["result"] = {"error": "Сервер остановил запрос."}
                raise
            except Exception:
                LOG.exception("Web job %s %s failed", key[:8], kind)
                job["state"] = "error"
                job["result"] = {"error": "Сбой обработки запроса. Код диагностики: " + key[:8]}
            finally:
                job["finished"] = time.monotonic()
                LOG.info("Web job %s %s %s after %.1fs", key[:8], kind, job["state"], job["finished"] - job["started"])

        job["task"] = asyncio.create_task(run())
        return {"job_id": key, "state": "running"}

    def status(self, owner, key):
        self.prune()
        job = self.items.get(key)
        if not job or job["owner"] != owner:
            return {"error": "Запрос не найден: возможно, сервер перезапустился или результат устарел."}
        result = {"job_id": key, "state": job["state"], "elapsed": round(time.monotonic() - job["started"])}
        if job["state"] != "running":
            result["result"] = job["result"]
        return result

    async def shutdown(self):
        tasks = [j["task"] for j in self.items.values() if not j["task"].done()]
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
