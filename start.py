import asyncio
import logging
import os
import signal
import sys
from types import FrameType

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)


async def stop_processes(processes: list[asyncio.subprocess.Process]) -> None:
    running = [process for process in processes if process.returncode is None]
    for process in running:
        process.terminate()

    if not running:
        return

    try:
        await asyncio.wait_for(
            asyncio.gather(*(process.wait() for process in running)),
            timeout=10,
        )
    except TimeoutError:
        for process in running:
            if process.returncode is None:
                process.kill()
        await asyncio.gather(*(process.wait() for process in running))


async def main() -> None:
    port = os.getenv("PORT", "80")
    processes: list[asyncio.subprocess.Process] = []
    stop_requested = asyncio.Event()
    loop = asyncio.get_running_loop()

    def request_stop(_signal_number: int, _frame: FrameType | None) -> None:
        loop.call_soon_threadsafe(stop_requested.set)

    for handled_signal in (signal.SIGTERM, signal.SIGINT):
        signal.signal(handled_signal, request_stop)

    try:
        backend = await asyncio.create_subprocess_exec(
            sys.executable,
            "-m",
            "uvicorn",
            "main:app",
            "--host",
            "0.0.0.0",
            "--port",
            port,
        )
        processes.append(backend)
        logger.info("Started FastAPI backend on port %s", port)
        bot_environment = os.environ.copy()
        bot_environment.setdefault(
            "BACKEND_INTERNAL_URL", f"http://127.0.0.1:{port}"
        )
        bot = await asyncio.create_subprocess_exec(
            sys.executable, "bot.py", env=bot_environment
        )
        processes.append(bot)
        logger.info("Started Telegram bot process")

        process_waiters = {
            asyncio.create_task(process.wait()): process
            for process in processes
        }
        stop_waiter = asyncio.create_task(stop_requested.wait())
        done, pending = await asyncio.wait(
            [*process_waiters, stop_waiter],
            return_when=asyncio.FIRST_COMPLETED,
        )

        for waiter in pending:
            waiter.cancel()
        await asyncio.gather(*pending, return_exceptions=True)

        if stop_waiter not in done:
            exited_waiter = next(
                waiter for waiter in done if waiter in process_waiters
            )
            exited = process_waiters[exited_waiter]
            raise RuntimeError(
                f"{'Backend' if exited is backend else 'Telegram bot'} "
                f"exited unexpectedly with code {exited.returncode}"
            )
    finally:
        await stop_processes(processes)
        logger.info("Application processes stopped")


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
