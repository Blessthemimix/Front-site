from bot_app.verification import VerificationInput, compute_digit_value, extract_osu_identifier


def test_extract_identifier_from_url() -> None:
    assert extract_osu_identifier("https://osu.ppy.sh/users/12345") == "12345"
    assert extract_osu_identifier("https://osu.ppy.sh/users/name123?mode=osu") == "name123"


def test_rank_digit_count_matches_original_behavior() -> None:
    value = compute_digit_value(
        VerificationInput(osu_id=999, username="x", global_rank=12345),
        "rank_digit_count",
    )
    assert value == 5


def test_last_digit_mode() -> None:
    value = compute_digit_value(
        VerificationInput(osu_id=123456789, username="x", global_rank=None),
        "last_digit_of_userid",
    )
    assert value == 9


def test_sum_digits_mod_mode() -> None:
    value = compute_digit_value(
        VerificationInput(osu_id=1234, username="x", global_rank=None),
        "sum_of_digits_mod_X",
        digit_modulus=7,
    )
    assert value == (1 + 2 + 3 + 4) % 7
