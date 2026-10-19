"""On-robot announcement player (IT-24), uploaded to /home/kachaka/bio_announce.py.

The Pi pushes the WAV and a per-run config, then starts ``python3 -u
bio_announce.py <run_id>`` detached; from there this script needs no network:
raise the speaker to the config volume, play the clip, put the robot's own
volume back.

Runs in the robot's Playground container: Python 3.9 + stdlib + the
pre-installed ``kachaka_api`` only. Keep it free of 3.10+ syntax and of any
bio-patrol import.

Volume safety:
- an flock serialises overlapping announcements, so one never "restores" to
  another's raised volume;
- the original volume is written (fsynced) to a residue file before it is
  changed and deleted only after a confirmed restore — a run that was killed
  mid-way leaves the residue behind and the next run restores to that value
  instead of the (still raised) current one, as long as the robot still sits
  at the raised volume; a volume set by hand since then wins over the residue;
- a volume that cannot be read means nothing is played;
- ``signal.alarm`` without a handler is the hard deadline: the kernel ends a
  hung run and the residue covers the restore.
"""
import fcntl
import hashlib
import json
import os
import signal
import sys
import time

import kachaka_api

pb2 = kachaka_api.pb2

GRPC_CANDIDATES = ("100.94.1.1:26400", "localhost:26400")
CONFIG_PATH_TMPL = "/home/kachaka/bio_announce_config-{}.json"
RESIDUE_PATH = "/home/kachaka/bio_announce_restore.json"
LOCK_PATH = "/home/kachaka/bio_announce.lock"
RPC_TIMEOUT = 5
RESTORE_ATTEMPTS = 3

# Read by the SIGTERM handler, which can fire between any two lines of run().
_state = {"client": None, "orig": None, "changed": False, "exiting": False,
          "exit_fn": os._exit}


def log(*args):
    """Only output channel; the Pi redirects stdout to /tmp/bio_announce-<rid>.log."""
    print(time.strftime("%Y-%m-%d %H:%M:%S"), "[bio_announce]", *args, flush=True)


def _connect():
    """First candidate that answers GetRobotSerialNumber wins."""
    last_err = None
    for addr in GRPC_CANDIDATES:
        try:
            client = kachaka_api.KachakaApiClient(addr)
            client.stub.GetRobotSerialNumber(pb2.GetRequest(), timeout=3)
            log("connected via", addr)
            return client
        except Exception as exc:
            last_err = exc
            log("connect failed via", addr, ":", exc)
    raise RuntimeError("no reachable gRPC endpoint: %s" % last_err)


def load_config(rid):
    """Read this run's config and delete it — it is single-use."""
    path = CONFIG_PATH_TMPL.format(rid)
    with open(path) as f:
        cfg = json.load(f)
    os.remove(path)
    return cfg


def _original_volume(client, raised):
    """Residue value if a previous run was cut short and left the robot at
    ``raised``, else the current volume. None when the volume cannot be read."""
    try:
        vol = client.stub.GetSpeakerVolume(pb2.GetRequest(), timeout=RPC_TIMEOUT).volume
    except Exception as exc:
        log("GetSpeakerVolume failed:", exc)
        return None
    if os.path.exists(RESIDUE_PATH):
        try:
            with open(RESIDUE_PATH) as f:
                res = int(json.load(f)["volume"])
            if vol == raised:
                log("residue found, original volume", res)
                return res
            log("residue", res, "ignored: volume is", vol, "not", raised, "(set since)")
        except Exception as exc:
            log("residue unreadable, using current volume:", exc)
    log("original volume", vol)
    return vol


def _write_residue(vol):
    tmp = RESIDUE_PATH + ".tmp"
    with open(tmp, "w") as f:
        json.dump({"volume": vol}, f)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, RESIDUE_PATH)
    # The rename itself must reach disk too: this file outlives a power cut.
    dir_fd = os.open(os.path.dirname(RESIDUE_PATH), os.O_RDONLY)
    try:
        os.fsync(dir_fd)
    finally:
        os.close(dir_fd)
    log("residue written", vol)


def _restore(client, vol):
    """Put the volume back; the residue goes only once the robot confirms."""
    for attempt in range(1, RESTORE_ATTEMPTS + 1):
        try:
            resp = client.stub.SetSpeakerVolume(
                pb2.SetSpeakerVolumeRequest(volume=vol), timeout=RPC_TIMEOUT)
            if resp.result.success:
                _state["changed"] = False
                try:
                    os.remove(RESIDUE_PATH)
                except FileNotFoundError:
                    pass
                log("volume restored to", vol, "(attempt %d)" % attempt)
                return True
            log("restore attempt", attempt, "rejected:", resp.result.error_code)
        except Exception as exc:
            log("restore attempt", attempt, "failed:", exc)
        if attempt < RESTORE_ATTEMPTS:
            time.sleep(1)
    log("restore FAILED, residue kept for the next run")
    return False


def _sound_id(client, base, data):
    """Robot sound id for this clip, named like services/sounds.py:sound_key so
    a clip the Pi already uploaded (e.g. arrival_zh) is reused."""
    name = base + "-" + hashlib.sha256(data).hexdigest()[:8]
    sounds = client.stub.GetSoundList(pb2.GetRequest(), timeout=RPC_TIMEOUT).sounds
    for s in sounds:
        if s.name == name:
            log("sound exists", name, "->", s.id)
            return s.id
    resp = client.stub.AddSound(pb2.AddSoundRequest(name=name, data=data),
                                timeout=RPC_TIMEOUT)
    if not resp.result.success:
        log("AddSound rejected", name, ":", resp.result.error_code)
        return None
    log("sound added", name, "->", resp.sound_id)
    return resp.sound_id


def _play(cfg, client):
    """Returns the exit code. Runs with the lock held and the alarm armed."""
    orig = _original_volume(client, cfg["volume"])
    if orig is None:
        log("volume unreadable, not playing")
        return 1
    _write_residue(orig)
    _state["orig"] = orig
    _state["changed"] = True
    try:
        with open(cfg["sound_file"], "rb") as f:
            data = f.read()
        sid = _sound_id(client, cfg["sound_base"], data)
        if not sid:
            return 1
        resp = client.stub.SetSpeakerVolume(
            pb2.SetSpeakerVolumeRequest(volume=cfg["volume"]), timeout=RPC_TIMEOUT)
        log("set volume", cfg["volume"], "success:", resp.result.success)
        if cfg.get("pre_delay_s"):
            time.sleep(cfg["pre_delay_s"])
        resp = client.stub.PlaySound(pb2.PlaySoundRequest(sound_id=sid, loop=False),
                                     timeout=RPC_TIMEOUT)
        if not resp.result.success:
            log("PlaySound rejected:", resp.result.error_code)
            return 1
        # BIO-PLAYING: robot_deployer.launch waits for this line in the log.
        log("BIO-PLAYING", sid, "for", cfg["seconds"], "s")
        time.sleep(cfg["seconds"] + 0.5)
        return 0
    except Exception as exc:
        log("announce failed:", exc)
        return 1
    finally:
        _restore(client, orig)


def run(cfg, client, exit_fn=os._exit):
    """Serialise on the lock, play, restore, then ``exit_fn(code)`` — os._exit
    on the robot so lingering gRPC threads cannot keep the process alive."""
    _state["client"] = client
    _state["exit_fn"] = exit_fn
    lock_fd = os.open(LOCK_PATH, os.O_CREAT | os.O_RDWR, 0o644)
    fcntl.flock(lock_fd, fcntl.LOCK_EX)
    # Armed only now: time spent queued behind another announcement must not count.
    signal.alarm(int(cfg["seconds"]) + 60)
    log("lock acquired")
    code = 1
    try:
        code = _play(cfg, client)
    except Exception as exc:
        log("unexpected error:", exc)
    log("exit", code)
    exit_fn(code)


def _on_sigterm(signum, frame):
    """Restore only if this run raised the volume; repeated SIGTERMs are ignored."""
    if _state["exiting"]:
        log("SIGTERM again, already leaving")
        return
    _state["exiting"] = True
    log("SIGTERM")
    if _state["changed"]:
        _restore(_state["client"], _state["orig"])
    _state["exit_fn"](0)


def main():
    rid = sys.argv[1]
    log("start", rid)
    signal.signal(signal.SIGTERM, _on_sigterm)
    try:
        cfg = load_config(rid)
        log("config", cfg)
        client = _connect()
    except Exception as exc:
        log("startup failed:", exc)
        os._exit(1)
    run(cfg, client)


if __name__ == "__main__":
    main()
