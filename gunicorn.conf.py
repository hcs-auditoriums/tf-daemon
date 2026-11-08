# gunicorn config for tf-daemon.
#   gunicorn -c gunicorn.conf.py tf_daemon.wsgi:app
#
# ONE worker only: the audio device can be opened exactly once, and all
# analysis state lives in that process. Concurrency comes from threads; each
# open WebSocket occupies one thread, so `threads` bounds the number of
# simultaneous browser clients (minus one or two for plain HTTP).
import os

bind = os.environ.get("TF_DAEMON_BIND", "0.0.0.0:8080")
workers = 1
worker_class = "gthread"
threads = int(os.environ.get("TF_DAEMON_THREADS", "8"))
preload_app = False  # open the device in the worker, never in the master
timeout = 30
graceful_timeout = 5
accesslog = None
errorlog = "-"
loglevel = "info"


def on_starting(server):
    if server.cfg.workers != 1:
        raise SystemExit("tf-daemon must run with exactly one gunicorn worker (-w 1)")


def worker_exit(server, worker):
    try:
        from tf_daemon.wsgi import app
    except Exception:
        return
    stop = app.extensions.get("tf_stop")
    if stop:
        stop()
