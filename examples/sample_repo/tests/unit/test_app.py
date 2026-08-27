from src.app import main

def test_main():
    assert main()["status"] == "ok"
