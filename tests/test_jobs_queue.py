"""Очередь нарезки не должна вставать навсегда.

Симптом, ради которого написано: этаж бесконечно висит «В очереди», а
страница крутит загрузку.
"""
import sys, threading, time, unittest
from pathlib import Path
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "webapp"))

from app import jobs


class Queue(unittest.TestCase):
    def setUp(self):
        jobs._queue = jobs.queue.Queue()
        jobs._worker_thread = None

    def tearDown(self):
        jobs._worker_thread = None

    def wait_for(self, check, limit=5.0):
        end = time.time() + limit
        while time.time() < end:
            if check():
                return True
            time.sleep(0.02)
        return False

    def test_dead_worker_is_replaced(self):
        # Мёртвый поток раньше оставлял поднятым флаг «уже запускали», и
        # следующий этаж не начинал резаться никогда.
        dead = threading.Thread(target=lambda: None)
        dead.start()
        dead.join()
        jobs._worker_thread = dead
        jobs.start_worker()
        self.assertIsNot(jobs._worker_thread, dead)
        self.assertTrue(jobs._worker_thread.is_alive())

    def test_live_worker_is_not_duplicated(self):
        jobs.start_worker()
        first = jobs._worker_thread
        jobs.start_worker()
        self.assertIs(jobs._worker_thread, first)

    def test_worker_survives_failure_and_takes_the_next_floor(self):
        # Падает и сама нарезка, и попытка записать ошибку в базу — раньше
        # это выбрасывало исключение наружу и убивало поток.
        seen = []

        def run(floor_id):
            seen.append(floor_id)
            raise RuntimeError("нарезка упала")

        def execute(*a, **k):
            raise RuntimeError("база заблокирована")

        old_run, old_execute = jobs._run, jobs.db.execute
        jobs._run, jobs.db.execute = run, execute
        try:
            jobs.start_worker()
            jobs._queue.put(1)
            jobs._queue.put(2)
            self.assertTrue(self.wait_for(lambda: seen == [1, 2]), seen)
            self.assertTrue(jobs._worker_thread.is_alive())
        finally:
            jobs._run, jobs.db.execute = old_run, old_execute


if __name__ == "__main__":
    unittest.main()
