from services.notifications.events import Source

def test_offline_and_recovered_sources_exist():
    assert Source.ROBOT_OFFLINE.value == "robot_offline"
    assert Source.ROBOT_RECOVERED.value == "robot_recovered"
