import os

import pytest
import yaml

from dealhawk import cli
from dealhawk.config import ConfigError, load_config, parse_config
from dealhawk.engine import Engine
from dealhawk.scheduler import Scheduler
from dealhawk.sources.base import Source
from dealhawk.store import Store

EXAMPLE = cli.EXAMPLE


def test_example_config_loads(tmp_path, monkeypatch):
    for k in ("NTFY_TOPIC", "WHATSAPP_TO"):
        monkeypatch.delenv(k, raising=False)
    cfg = load_config(EXAMPLE)
    names = [s.name for s in cfg.sources]
    assert names == ["walmart", "bestbuy-pages", "newegg"]          # verified first-party sources only
    assert all(s.trust == "first-party" for s in cfg.sources)
    assert [w.name for w in cfg.watches] == ["MacBooks", "GPUs & monitors", "Anything insane"]
    assert cfg.alerts.quiet_hours.timezone == "America/New_York"
    assert cfg.verify.tiers == ["steal"] and cfg.learning.enabled and not cfg.llm.enabled
    assert cfg.database.endswith("dealhawk.db") and os.path.isabs(cfg.database)


def test_env_expansion_and_errors(monkeypatch):
    monkeypatch.setenv("TOPIC", "abc")
    cfg = parse_config({"notify": [{"type": "ntfy", "topic": "${TOPIC}"}]}, env=True)
    assert cfg.notifiers[0].topic == "abc"
    assert parse_config({"database": "${NOPE:-x.db}"}).database == "x.db"
    with pytest.raises(ConfigError, match="NOPE2"):
        parse_config({"database": "${NOPE2}"})
    with pytest.raises(ConfigError, match="unknown top-level"):
        parse_config({"sorces": []})
    with pytest.raises(ConfigError, match="type must be one of"):
        parse_config({"sources": [{"type": "magic"}]})
    with pytest.raises(ConfigError, match="subredit"):
        parse_config({"sources": [{"type": "reddit", "subreddits": ["a"], "subredit": "typo"}]})
    with pytest.raises(ConfigError, match="duplicate"):
        parse_config({"sources": [{"type": "rss", "url": "https://a"}, {"type": "rss", "url": "https://b",
                                                                        "name": "rss-0"}]})
    with pytest.raises(ConfigError, match="watches\\[0\\]"):
        parse_config({"watches": [{"name": "x", "keyword": ["typo"]}]})
    with pytest.raises(ConfigError, match="secret"):
        parse_config({"feedback": {"public_url": "https://x"}})
    with pytest.raises(ConfigError, match="whatsapp"):
        parse_config({"notify": [{"type": "whatsapp", "provider": "twilio", "to": "+1"}]})
    cfg = parse_config({"watches": [{"name": "m", "keywords": "macbook"}]})
    assert cfg.watches[0].keywords == ["macbook"]


class Flaky(Source):
    def __init__(self, name, fail_times=0, offers=None):
        super().__init__(name, interval=60)
        self.fail_times, self.offers_list, self.calls = fail_times, offers or [], 0

    def fetch(self):
        self.calls += 1
        if self.calls <= self.fail_times:
            raise RuntimeError("boom")
        return self.offers_list


def test_scheduler_runs_due_sources_and_backs_off():
    t = [1000.0]
    cfg = parse_config({})
    eng = Engine(cfg, Store(":memory:"), notifiers=[])
    ok, bad = Flaky("ok"), Flaky("bad", fail_times=5)
    sch = Scheduler(eng, [ok, bad], clock=lambda: t[0], sleep=lambda s: t.__setitem__(0, t[0] + s))
    from concurrent.futures import ThreadPoolExecutor
    with ThreadPoolExecutor(2) as pool:
        t[0] += 5
        sch.tick(pool)
        assert ok.calls == 1 and bad.calls == 1 and sch.failures["bad"] == 1
        assert sch.next_run["bad"] - t[0] > sch.next_run["ok"] - t[0]       # backed off
        t[0] += 61 * 1.1
        sch.tick(pool)
        assert ok.calls == 2 and bad.calls == 1                             # bad still waiting
    sch.run(max_ticks=2)                                                    # loop + stop path


def test_cli_end_to_end(tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    cli.main(["init"])
    raw = yaml.safe_load(open("dealhawk.yaml"))
    raw["sources"] = [{"type": "rss", "name": "feed", "url": "file://unused"}]
    yaml.safe_dump(raw, open("dealhawk.yaml", "w"))
    cli.main(["sources"])
    out = capsys.readouterr().out
    assert "retailer" in out and "slickdeals" not in out and "<RssSource feed" in out
    cli.main(["once"])                                                       # network blocked → reported
    assert "✗ feed" in capsys.readouterr().out
    cli.main(["score", "20", "--history", "100,100,95,100,60,100,98,62,100"])
    assert "score 100/100 → steal" in capsys.readouterr().out
    cli.main(["deals"])
    cli.main(["brain"])
    assert "labels: you 0" in capsys.readouterr().out
    with pytest.raises(SystemExit):
        cli.main(["feedback", "999", "good"])
    with pytest.raises(SystemExit):
        cli.main(["init"])                                                  # refuses to overwrite
    with pytest.raises(SystemExit):
        cli.main(["webhooks"])                                              # feedback not configured
