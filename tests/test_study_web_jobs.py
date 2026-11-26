import asyncio
import unittest
from study_web_jobs import JobStore

class JobTests(unittest.IsolatedAsyncioTestCase):
    async def test_lifecycle_and_isolation(self):
        store=JobStore(max_active=1)
        gate=asyncio.Event()
        async def work():
            await gate.wait()
            return {"answer":"ok"}
        owner=(1,"a")
        key=store.start(owner,"ask",work)["job_id"]
        self.assertEqual(store.status(owner,key)["state"],"running")
        self.assertIn("error",store.status((2,"a"),key))
        self.assertIn("error",store.status((1,"b"),key))
        self.assertIn("error",store.start(owner,"export",work))
        self.assertIn("error",store.start((2,"b"),"ask",work))
        gate.set()
        await store.items[key]["task"]
        self.assertEqual(store.status(owner,key)["result"],{"answer":"ok"})
        self.assertEqual(store.status(owner,key)["state"],"done")
        self.assertIn("job_id",store.start(owner,"export",work))
        await store.shutdown()

    async def test_error_and_expiry(self):
        store=JobStore(ttl=0)
        async def work():
            return {"error":"failed"}
        key=store.start((1,"a"),"ask",work)["job_id"]
        await store.items[key]["task"]
        self.assertEqual(store.items[key]["state"],"error")
        self.assertIn("error",store.status((1,"a"),key))

    async def test_exception(self):
        store=JobStore()
        async def work():
            raise RuntimeError("private details")
        key=store.start((1,"a"),"ask",work)["job_id"]
        await store.items[key]["task"]
        result=store.status((1,"a"),key)
        self.assertEqual(result["state"],"error")
        self.assertNotIn("private details",result["result"]["error"])

if __name__=="__main__": unittest.main()
