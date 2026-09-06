from common.throttles import IdentifierThrottle, IpThrottle


# Mirrors accounts' login pair (US-SEC2): per-IP catches credential stuffing across many
# accounts from one source; per-identifier catches one account hammered from many IPs.
# Neither queries the DB, so the enumeration asymmetry stays intact — the 429 is identical
# whether or not the submitted email belongs to a staff account.
class StaffLoginIpThrottle(IpThrottle):
    scope = "staff_login_ip"


class StaffLoginIdentifierThrottle(IdentifierThrottle):
    scope = "staff_login_identifier"
    field = "email"


class StaffOtpIpThrottle(IpThrottle):
    scope = "staff_otp_ip"
