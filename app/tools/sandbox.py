"""
POST /tools/execute-code  (contract §2.6)

Runs submitted code inside an ephemeral, network-disabled, resource-capped
Docker container and returns stdout/stderr/exit_code.

Air-gap note: network_disabled=True on every run is not optional — this is
part of the project's "zero external calls" story, not just a safety nicety.
"""
import tarfile
import io
import time
import uuid

import docker
from docker.errors import ContainerError, ImageNotFound, APIError

_LANGUAGE_IMAGES = {
    "python": "python:3.11-slim",
}

_LANGUAGE_CMD = {
    "python": lambda filename: ["python", filename],
}

_LANGUAGE_FILENAME = {
    "python": "snippet.py",
}

# Resource caps — tuned to leave headroom for the rest of the stack
# (inference server + vector DB) running on the same laptop (§4.6 "worst
# case" test in the master guide).
CPU_QUOTA = 50000       # 0.5 CPU (period default 100000)
CPU_PERIOD = 100000
MEM_LIMIT = "256m"
PIDS_LIMIT = 64
TIMEOUT_SECONDS = 15


class UnsupportedLanguage(Exception):
    pass


def _get_client() -> docker.DockerClient:
    return docker.from_env()


def _build_tar(filename: str, content: str) -> io.BytesIO:
    data = content.encode("utf-8")
    stream = io.BytesIO()
    with tarfile.open(fileobj=stream, mode="w") as tar:
        info = tarfile.TarInfo(name=filename)
        info.size = len(data)
        info.mtime = int(time.time())
        tar.addfile(info, io.BytesIO(data))
    stream.seek(0)
    return stream


def run_code(code: str, language: str) -> dict:
    """
    Returns: {"stdout": str, "stderr": str, "exit_code": int}
    Never raises for "the code itself failed" — that's a normal exit_code != 0.
    Raises UnsupportedLanguage for an unsupported `language` value (caller
    turns that into the flat {"error": ...} / 400 shape per §2.8).
    """
    if language not in _LANGUAGE_IMAGES:
        raise UnsupportedLanguage(f"Unsupported language: {language!r}")

    image = _LANGUAGE_IMAGES[language]
    filename = _LANGUAGE_FILENAME[language]
    cmd = _LANGUAGE_CMD[language](filename)

    client = _get_client()
    container_name = f"tools-exec-{uuid.uuid4().hex[:12]}"

    container = client.containers.create(
        image=image,
        command=["sleep", str(TIMEOUT_SECONDS + 5)],
        name=container_name,
        network_disabled=True,          # air-gapped: no network from inside the sandbox
        mem_limit=MEM_LIMIT,
        memswap_limit=MEM_LIMIT,        # disable swap growth beyond mem_limit
        cpu_period=CPU_PERIOD,
        cpu_quota=CPU_QUOTA,
        pids_limit=PIDS_LIMIT,
        working_dir="/sandbox",
        detach=True,
        auto_remove=False,
    )

    try:
        container.start()

        # Write the code into the container as a tar stream (no shared bind
        # mount needed — keeps the container fully isolated from host FS).
        container.exec_run(["mkdir", "-p", "/sandbox"])
        tar_stream = _build_tar(filename, code)
        container.put_archive("/sandbox", tar_stream)

        exec_result = container.exec_run(
            cmd,
            workdir="/sandbox",
            demux=True,
        )
        exit_code = exec_result.exit_code
        stdout_bytes, stderr_bytes = exec_result.output
        stdout = (stdout_bytes or b"").decode("utf-8", errors="replace")
        stderr = (stderr_bytes or b"").decode("utf-8", errors="replace")

        return {
            "stdout": stdout,
            "stderr": stderr,
            "exit_code": exit_code if exit_code is not None else -1,
        }
    except ContainerError as e:
        return {"stdout": "", "stderr": str(e), "exit_code": e.exit_status or 1}
    finally:
        try:
            container.kill()
        except APIError:
            pass
        try:
            container.remove(force=True)
        except APIError:
            pass


def check_docker_available() -> bool:
    try:
        _get_client().ping()
        return True
    except Exception:
        return False
