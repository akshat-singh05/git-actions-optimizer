from src.api import routes

def test_health():
    assert routes.health()["status"] == "ok"
