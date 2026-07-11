import subprocess

from hark import audio


def test_probe_duration_parses_ffprobe_output(monkeypatch):
    def fake_run(cmd, **kwargs):
        assert cmd[0] == "ffprobe"
        assert cmd[-1] == "/tmp/fake.mp3"
        return subprocess.CompletedProcess(cmd, 0, stdout="123.456\n", stderr="")

    monkeypatch.setattr(subprocess, "run", fake_run)
    assert audio.probe_duration("/tmp/fake.mp3") == 123.456
