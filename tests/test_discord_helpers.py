from bot_app.discord_client import get_all_digit_role_ids


def test_flatten_role_ids() -> None:
    role_mapping = {"osu": {4: 1, 5: 2}, "mania": {6: 3}}
    assert get_all_digit_role_ids(role_mapping) == {1, 2, 3}
