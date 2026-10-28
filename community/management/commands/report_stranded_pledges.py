"""Backlog · report pledges stranded by accounts deleted before backend#110. READ-ONLY.

    python manage.py report_stranded_pledges [--limit 20] [--json]

Run it on staging or production and read the numbers before deciding whether a data migration is worth
writing: the fix is irreversible (a settled pledge cannot be un-settled), so the decision is a person's,
made from real counts. This command changes nothing. See `community/audit.py` for what it looks for.
"""
import json

from django.core.management.base import BaseCommand

from community.audit import DEFAULT_LIMIT, stranded_pledge_report


class Command(BaseCommand):
    help = "Report pledges and needs stranded by accounts deleted before #110. Changes nothing."

    def add_arguments(self, parser):
        parser.add_argument("--limit", type=int, default=DEFAULT_LIMIT,
                            help="rows to list per group (the counts are always complete)")
        parser.add_argument("--json", action="store_true", help="machine-readable output")

    def handle(self, *args, **options):
        report = stranded_pledge_report(limit=options["limit"])
        if options["json"]:
            self.stdout.write(json.dumps(report, indent=2))
            return

        s = report["scanned"]
        self.stdout.write(f"scanned: {s['deleted_accounts']} deleted account(s), {s['waiting_pledges']} waiting "
                          f"pledge(s), {s['open_or_fulfilled_needs']} open or fulfilled need(s)")
        groups = [
            ("donor_pledges", "waiting pledges from deleted donors (would end as donor_left)"),
            ("shelter_needs", "open or still-holding needs of deleted shelters (would be closed)"),
            ("shelter_need_pledges", "waiting pledges on those shelters' needs (would end as need_closed)"),
        ]
        for key, label in groups:
            group = report[key]
            self.stdout.write(f"{group['count']:>6}  {label}")
            for row in group["rows"]:
                self.stdout.write("          " + "  ".join(f"{k}={v}" for k, v in row.items()))
            if group["count"] > len(group["rows"]):
                self.stdout.write(f"          … and {group['count'] - len(group['rows'])} more (raise --limit)")
        if report["pledges_to_settle"] == 0 and report["shelter_needs"]["count"] == 0:
            self.stdout.write(self.style.SUCCESS("nothing to settle (see the scanned totals above for what that is out of)"))
        else:
            self.stdout.write(self.style.WARNING(
                f"{report['pledges_to_settle']} distinct pledge(s) and {report['shelter_needs']['count']} need(s) "
                "would be settled."))
        self.stdout.write("This command changed nothing.")
