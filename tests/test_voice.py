"""Tests: Stimmen klingen natürlicher (Text, Stimmprobe, Lautstärke)."""
from tests.test_core import _cfg


def test_voices_sound_more_human():
    import numpy as np
    from coach.audio import tts as T
    # Abkürzungen werden so gesagt, wie ein Mensch sie sagt
    assert T.speakable("z.B. 2v1 auf HP, ca. 80%") == "zum Beispiel 2 gegen 1 auf Hardpoint, circa 80 Prozent"
    assert T.speakable("3 vs 2 & SnD", "en") == "3 v 2 and Search and Destroy"
    assert T.xtts_text("Gut gemacht, bzw. fast") == "Gut gemacht, beziehungsweise fast."
    # Sampling nicht mehr so monoton wie früher (0.65)
    t = T.TTS(_cfg(tts={"engine": "xtts"}))
    samp = t.xtts_sampling()
    assert 0.7 <= samp["temperature"] <= 0.8 and samp["top_p"] == 0.85
    # Stimmprobe: lange Pausen gekürzt, Lautstärke nach Sprache, kein Übersteuern
    rate = 24000
    tone = lambda sec, amp: (amp * np.sin(np.linspace(0, 2 * np.pi * 180 * sec, int(rate * sec)))).astype(np.float32)
    raw = np.concatenate([tone(2, 0.02), np.zeros(rate * 3, np.float32), tone(2, 0.02)])  # leise + 3 s Stille
    ref = T.clean_reference(raw, rate)
    assert len(ref) < 4.6 * rate  # 3 s Pause auf ~0,35 s gekürzt
    assert 0.08 < float(np.sqrt(np.mean(ref[: rate] ** 2))) < 0.12 and float(np.abs(ref).max()) <= 0.95
    # zwei unterschiedlich laute Aufnahmen werden gleich laut
    a, b = T.polish(tone(1, 0.06), rate), T.polish(tone(1, 0.5), rate)
    ra, rb = (float(np.sqrt(np.mean(x[rate // 10:] ** 2))) for x in (a, b))
    assert abs(ra - rb) / rb < 0.1 and float(np.abs(b).max()) <= 0.97


def test_custom_voice_uses_cleaned_reference(tmp_path, monkeypatch):
    import wave

    import numpy as np
    from coach.audio import tts as T
    monkeypatch.setattr(T, "CACHE", tmp_path)
    src = tmp_path / "daniel.wav"
    rate = 24000
    a = (0.3 * np.sin(np.linspace(0, 2 * np.pi * 150 * 5, rate * 5))).astype(np.float32)
    with wave.open(str(src), "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(rate)
        wf.writeframes((a * 32767).astype("<i2").tobytes())
    ref = T.TTS._clean_ref_file(src)
    assert ref != src and ref.exists() and ref.parent == tmp_path
    bad = tmp_path / "kaputt.wav"
    bad.write_bytes(b"x")
    assert T.TTS._clean_ref_file(bad) == bad  # kaputte Datei -> Original nehmen statt abstürzen
