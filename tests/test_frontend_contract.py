from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def test_job_discovery_experience_contract() -> None:
    html = (ROOT / "web" / "index.html").read_text(encoding="utf-8")
    script = (ROOT / "web" / "assets" / "app.js").read_text(encoding="utf-8")
    styles = (ROOT / "web" / "assets" / "styles.css").read_text(encoding="utf-8")

    for control_id in (
        "radarResultQuery",
        "radarResultSort",
        "radarSavedOnly",
        "radarFilterReset",
        "radarFilterEmpty",
    ):
        assert f'id="{control_id}"' in html

    assert 'class="mobile-bottom-nav"' in html
    assert "SAVED_JOBS_STORAGE_KEY" in script
    assert "window.localStorage.setItem" in script
    assert 'event.key === "/"' in script
    assert "openApplicationDialog(null, job)" in script
    assert ".has-results .workflow-strip" in styles
    assert "@media (max-width: 640px)" in styles


def test_frontend_assets_are_versioned_with_release() -> None:
    html = (ROOT / "web" / "index.html").read_text(encoding="utf-8")

    assert "/assets/styles.css?v=1.2.0-ui1" in html
    assert "/assets/app.js?v=1.2.0-ui1" in html
    assert "<span>v1.2</span>" in html
