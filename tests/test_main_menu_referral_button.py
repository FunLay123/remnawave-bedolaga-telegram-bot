from app.config import settings
from app.keyboards.inline import get_main_menu_keyboard


def test_referral_button_is_full_width_without_primary(monkeypatch):
    monkeypatch.setattr(settings, 'REFERRAL_PROGRAM_ENABLED', True)
    monkeypatch.setattr(settings, 'SUPPORT_MENU_ENABLED', True)

    keyboard = get_main_menu_keyboard(language='ru')
    rows = keyboard.inline_keyboard
    referral_index = next(
        index
        for index, row in enumerate(rows)
        if any(button.callback_data == 'menu_referrals' for button in row)
    )
    referral_row = rows[referral_index]

    assert len(referral_row) == 1
    button = referral_row[0]
    assert button.text == 'Партнёрка'
    assert button.icon_custom_emoji_id == '5343636681473935403'
    assert button.style is None

    support_indexes = [
        index
        for index, row in enumerate(rows)
        if any(button.callback_data == 'menu_support' for button in row)
    ]
    if support_indexes:
        assert referral_index < support_indexes[0]
