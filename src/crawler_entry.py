"""Cron and queue entrypoint for the artist-analytics-crawler Worker.

This file is the Workers `main`, same layout as `src/main.py`: repo-root
`wrangler.crawler.jsonc` points at `src/crawler_entry.py`. The `workers`
module is injected by the Python Workers runtime only for that layout.
A config in a subdirectory with `main` outside that directory bundled the
file as a plain script, and startup failed with
`ModuleNotFoundError: No module named 'workers'`.

Importing this module does not import FastAPI, Jinja, or a provider parser.
Those load inside the single message that needs them.
"""

from __future__ import annotations

try:
    from workers import Response, WorkerEntrypoint
except ImportError:  # Local tests have no Workers runtime. Deploy has it.

    class WorkerEntrypoint:
        def __init__(self, ctx=None, env=None):
            self.ctx = ctx
            self.env = env

    Response = None


class Default(WorkerEntrypoint):
    async def fetch(self, request):
        if Response is None:
            return None
        return Response("artist-analytics-crawler does not serve the dashboard.", status=404)

    async def scheduled(self, controller=None, env=None, ctx=None):
        from crawl import _exc_fields, _log, handler_env, run_scheduled

        resolved = handler_env(self, env)
        _log("scheduled start", cron=getattr(controller, "cron", None))
        try:
            result = await run_scheduled(resolved)
        except Exception as exc:
            _log("scheduled error", **_exc_fields(exc))
            raise
        _log(
            "scheduled end",
            status=result.get("status"),
            attempted=result.get("attempted", 0),
            ok=result.get("ok", 0),
            error=result.get("error", 0),
            reason=result.get("reason"),
        )
        return result

    async def queue(self, batch=None, env=None, ctx=None):
        from crawl import (
            RETRY_DELAY_SECONDS,
            _exc_fields,
            _finish_message,
            _log,
            handler_env,
            run_queue_batch,
        )

        resolved = handler_env(self, env)
        messages = list(getattr(batch, "messages", None) or [])
        _log("queue start", messages=len(messages))
        failure = None
        try:
            result = await run_queue_batch(batch, resolved)
        except Exception as exc:
            # Do not await message.retry until this exception has finished propagating.
            failure = _exc_fields(exc)
        if failure is not None:
            _log("queue error", **failure)
            for message in messages:
                await _finish_message(message, "retry", delay_seconds=RETRY_DELAY_SECONDS)
            result = {"messages": len(messages), "acked": 0, "retried": len(messages), "snapshots": 0}
        _log(
            "queue end",
            messages=result.get("messages", 0),
            acked=result.get("acked", 0),
            retried=result.get("retried", 0),
            snapshots=result.get("snapshots", 0),
        )
        return result
