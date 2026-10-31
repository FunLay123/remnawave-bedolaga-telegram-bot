from __future__ import annotations


_CLIENT_APP_MARKERS = (
    ('happ', 'Happ'),
    ('streisand', 'Streisand'),
    ('v2raytun', 'v2RayTun'),
    ('v2rayng', 'v2rayNG'),
    ('hiddify', 'Hiddify'),
    ('clash', 'Clash'),
    ('mihomo', 'Mihomo'),
    ('shadowrocket', 'Shadowrocket'),
    ('v2box', 'V2Box'),
    ('nekobox', 'NekoBox'),
    ('karing', 'Karing'),
    ('foxray', 'Foxray'),
    ('incy', 'INCY'),
)


def client_app_name(device: dict) -> str | None:
    """Достаёт читаемое имя клиента из userAgent устройства.

    Панель отдаёт ``userAgent`` строкой; в ней обычно есть имя клиента
    (Happ, Streisand, v2rayNG…). Неизвестный UA показываем первым токеном,
    чтобы не выводить длинную техническую строку.
    """
    raw = device.get('userAgent') or device.get('user_agent')
    user_agent = str(raw or '').strip()
    if not user_agent:
        return None

    lowered = user_agent.lower()
    if lowered.startswith(('sfa/', 'sfi/', 'sfm/')):
        return 'sing-box'

    for marker, name in _CLIENT_APP_MARKERS:
        if marker in lowered:
            return name

    if lowered.startswith('mozilla'):
        return 'Браузер'

    token = user_agent.split('/', 1)[0].split(' ', 1)[0].strip()
    return token[:24] or None


def device_display_name(device: dict) -> str:
    """Человекочитаемое имя устройства для кнопки/списка.

    Локальный alias важнее платформенной строки. Клиент добавляется только
    если он ещё не упомянут в имени.
    """
    local_name = (device.get('local_name') or '').strip()
    if local_name:
        name = local_name
    else:
        platform = device.get('platform') or device.get('platformType') or 'Unknown'
        model = device.get('deviceModel') or device.get('model') or device.get('name') or 'Unknown'
        name = f'{platform} - {model}'

    app_name = client_app_name(device)
    if app_name and app_name.lower() not in name.lower():
        name = f'{name} · {app_name}'
    return name
