from app.tools.referral_days_only_backfill import build_parser


def test_parser_requires_explicit_apply_flag():
    parser = build_parser()
    args = parser.parse_args([])
    assert args.apply is False

    args = parser.parse_args(['--apply'])
    assert args.apply is True
