from app.services.bookmakers.plugins.pinnacle.live_text import clean_pinnacle_league


def test_clean_pinnacle_league_removes_selection_limit_notice():
    assert clean_pinnacle_league("墨西哥 - 足球甲级联赛 您已达到选择的比赛数量上限200场") == "墨西哥 - 足球甲级联赛"


def test_clean_pinnacle_league_removes_selection_notice_with_direction_marks():
    assert (
        clean_pinnacle_league(
            "墨西哥 - 足球甲级联赛\u200e 您已达到选择的比赛数量上限\u200f200场"
        )
        == "墨西哥 - 足球甲级联赛"
    )


def test_clean_pinnacle_league_preserves_regular_league():
    assert clean_pinnacle_league("阿根廷 - 职业联赛") == "阿根廷 - 职业联赛"
