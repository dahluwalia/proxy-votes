"""M0.1: every model round-trips through JSON; id helpers."""

from datetime import UTC, date, datetime

import pytest
from pydantic import ValidationError

from pennant.core.models import (
    CHAppointment,
    CHOfficer,
    Decision,
    Evidence,
    Meeting,
    OrgFacts,
    Person,
    PositionFacts,
    Recommendation,
    ReviewItem,
    RoleType,
    RosterEntry,
    Seat,
    SeatStatus,
    StageReport,
    Vote,
    meeting_id,
    mention_id,
    notice_ref,
    pad_company_number,
    parse_notice_ref,
    person_id,
)

EV = Evidence(
    source="notice",
    ref=notice_ref("Tesco_2026_Notice_of_AGM.pdf", 10),
    quote="Chair: Barratt Redrow plc and ICE Clear Europe",
)
WEB = Evidence(
    source="web",
    ref="https://example.com/x",
    quote="admitted to trading",
    fetched_at=datetime(2026, 6, 1, tzinfo=UTC),
    title="Example",
)
MTG = meeting_id("445790", date(2026, 6, 18))
MEN = mention_id("445790", 4)

SAMPLES = [
    EV,
    WEB,
    Meeting(meeting_id=MTG, company_name="Tesco PLC", company_number="00445790",
            agm_date=date(2026, 6, 18), doc_sha="ab12", key="tesco"),
    RosterEntry(mention_id=MEN, meeting_id=MTG, resolution_no=4, name="Gerry Murphy",
                role_text_at_issuer="Chair", role_type_at_issuer=RoleType.chair,
                coverage="structured", evidence=[EV]),
    RosterEntry(mention_id="nm:04256886:r5", meeting_id="mtg:04256886:2026-07-07",
                resolution_no=5, name="Archie Norman", coverage="none"),
    Seat(seat_id=f"{MEN}:a1", mention_id=MEN, org_name="Barratt Redrow plc",
         org_key="barratt-redrow", ch_company_number="00604574", role_text="Chair",
         role_type=RoleType.chair, board_seat=True, chair="yes", executive="no",
         current="yes", status=SeatStatus.confirmed, evidence=[EV]),
    CHOfficer(officer_id="abc123", name="MURPHY, Gerard", surname="MURPHY",
              forenames="Gerard", dob_month=5, dob_year=1955,
              appointments=[CHAppointment(company_number="00445790", company_name="TESCO PLC",
                                          company_type="plc", role="director",
                                          appointed_on=date(2023, 1, 1))]),
    OrgFacts(org_key="capri", canonical_name="Capri Holdings Limited", org_type="company",
             listed="yes", market="NYSE", equity_listing="yes",
             events=["2025-04-14: sale of Versace announced"], evidence=[WEB]),
    PositionFacts(seat_id="s1", board_seat="unknown", executive="yes",
                  role_type=RoleType.officer_title, evidence=[WEB]),
    Person(person_id=person_id([MEN, "nm:04967001:r9"]), mention_ids=[MEN, "nm:04967001:r9"],
           officer_ids=["abc123"]),
    Recommendation(person_id="p:" + MEN, meeting_id=MTG, role_at_meeting=RoleType.chair,
                   points_lo=4, points_hi=4, points_rule="no", exec_rule="no", vote=Vote.FOR,
                   coverage="structured", reasons=["4 points, threshold 4"], evidence=[EV]),
    ReviewItem(kind="unanchored", subject=MEN, detail="no CH match",
               suggested_command="pennant merge ..."),
    Decision(decision_id="dec:3", kind="must_link", payload={"a": "x", "b": "y"},
             author="expert", reason="same DOB", created_at=datetime(2026, 10, 26, tzinfo=UTC)),
    StageReport(stage="extract", ok=True, counts={"roster": 50}),
]


@pytest.mark.parametrize("model", SAMPLES, ids=lambda m: type(m).__name__)
def test_json_round_trip(model):
    again = type(model).model_validate_json(model.model_dump_json())
    assert again == model
    assert again.model_dump_json() == model.model_dump_json()


def test_tri_fields_default_unknown_and_coerce_bools():
    s = Seat(seat_id="s", org_name="X", org_key="x", board_seat=True, listed=False)
    assert (s.board_seat, s.listed, s.chair, s.identity) == ("yes", "no", "unknown", "unknown")
    with pytest.raises(ValidationError):
        Seat(seat_id="s", org_name="X", org_key="x", current="maybe")


def test_meeting_company_number_must_be_8_digits():
    with pytest.raises(ValidationError):
        Meeting(meeting_id="m", company_name="X", company_number="445790",
                agm_date=date(2026, 1, 1), doc_sha="x")


def test_extra_fields_rejected():
    with pytest.raises(ValidationError):
        Person(person_id="p:x", mention_ids=["x"], nickname="oops")


def test_id_helpers():
    assert pad_company_number("4967001") == "04967001"
    assert pad_company_number(445790) == "00445790"
    assert pad_company_number("sc12345") == "SC012345"
    with pytest.raises(ValueError):
        pad_company_number("123456789")
    assert MTG == "mtg:00445790:2026-06-18"
    assert MEN == "nm:00445790:r4"
    assert person_id(["nm:04967001:r9", "nm:00445790:r4"]) == "p:nm:00445790:r4"
    assert parse_notice_ref(EV.ref) == ("Tesco_2026_Notice_of_AGM.pdf", 10)
    assert parse_notice_ref("https://x") == ("https://x", None)


def test_enum_values_match_spec():
    assert [r.value for r in RoleType] == [
        "executive_director", "executive_chair", "chair", "deputy_chair",
        "non_executive_director", "director_unspecified", "officer_title",
        "committee_chair", "adviser", "trustee", "other",
    ]
    assert [s.value for s in SeatStatus] == [
        "confirmed", "stale", "contradiction", "undisclosed", "ch_invisible",
        "ch_unavailable", "past",
    ]
    assert [v.value for v in Vote] == ["FOR", "AGAINST", "REVIEW"]
    assert Vote.FOR == "FOR"
