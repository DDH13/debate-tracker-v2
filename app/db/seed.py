from datetime import date

from sqlmodel import Session

from app.models import (
    Ballot,
    Debate,
    DebateJudge,
    Debater,
    Institution,
    Judge,
    Motion,
    Round,
    Side,
    SpeakerScore,
    SpeakerPosition,
    Team,
    TeamMember,
    Tournament,
)


def seed(session: Session) -> None:
    tournament = Tournament(name="Sample Open", date=date(2024, 6, 1), slug="sample-open")
    session.add(tournament)
    session.flush()

    institutions = [
        Institution(name="University A", code="UA"),
        Institution(name="University B", code="UB"),
        Institution(name="University C", code="UC"),
        Institution(name="University D", code="UD"),
    ]
    session.add_all(institutions)
    session.flush()

    teams = [
        Team(tournament_id=tournament.id, name="Team A", institution_id=institutions[0].id),
        Team(tournament_id=tournament.id, name="Team B", institution_id=institutions[1].id),
        Team(tournament_id=tournament.id, name="Team C", institution_id=institutions[2].id),
        Team(tournament_id=tournament.id, name="Team D", institution_id=institutions[3].id),
    ]
    session.add_all(teams)
    session.flush()

    debaters = [
        Debater(name="Alice Adams", email="alice@uni-a.edu", institution_id=institutions[0].id),
        Debater(name="Andrew Ali", institution_id=institutions[0].id),
        Debater(name="Amy Ito", institution_id=institutions[0].id),
        Debater(name="Bob Brooks", email="bob@uni-b.edu", institution_id=institutions[1].id),
        Debater(name="Bella Byrne", institution_id=institutions[1].id),
        Debater(name="Ben Barnes", institution_id=institutions[1].id),
        Debater(name="Cara Chen", institution_id=institutions[2].id),
        Debater(name="Carl Cole", institution_id=institutions[2].id),
        Debater(name="Cathy Cruz", institution_id=institutions[2].id),
        Debater(name="Dana Diaz", institution_id=institutions[3].id),
        Debater(name="Dave Dunn", institution_id=institutions[3].id),
        Debater(
            name="Sam Sharma",
            email="sam.sharma@example.com",
            institution_id=institutions[3].id,
        ),
    ]
    session.add_all(debaters)

    judges = [
        Judge(name="Jordan Park", email="jordan@judges.org", institution_id=institutions[1].id),
        Judge(name="Sam Sharma", email="sam.sharma@example.com"),
        Judge(name="Priya Patel", email="priya@judges.org", institution_id=institutions[2].id),
    ]
    session.add_all(judges)
    session.flush()

    session.add_all(
        [
            TeamMember(team_id=teams[0].id, debater_id=debaters[0].id),
            TeamMember(team_id=teams[0].id, debater_id=debaters[1].id),
            TeamMember(team_id=teams[0].id, debater_id=debaters[2].id),
            TeamMember(team_id=teams[1].id, debater_id=debaters[3].id),
            TeamMember(team_id=teams[1].id, debater_id=debaters[4].id),
            TeamMember(team_id=teams[1].id, debater_id=debaters[5].id),
            TeamMember(team_id=teams[2].id, debater_id=debaters[6].id),
            TeamMember(team_id=teams[2].id, debater_id=debaters[7].id),
            TeamMember(team_id=teams[2].id, debater_id=debaters[8].id),
            TeamMember(team_id=teams[3].id, debater_id=debaters[9].id),
            TeamMember(team_id=teams[3].id, debater_id=debaters[10].id),
            TeamMember(team_id=teams[3].id, debater_id=debaters[11].id),
        ]
    )

    round_1 = Round(tournament_id=tournament.id, seq=1, name="Round 1")
    round_2 = Round(tournament_id=tournament.id, seq=2, name="Round 2")
    session.add_all([round_1, round_2])
    session.flush()

    session.add_all(
        [
            Motion(round_id=round_1.id, text="This House Would ban social media for minors."),
            Motion(round_id=round_2.id, text="This House Would abolish standardized testing."),
        ]
    )

    debate_1 = Debate(
        round_id=round_1.id,
        prop_team_id=teams[0].id,
        opp_team_id=teams[1].id,
        room="Room 101",
        winner=Side.PROP,
    )
    debate_2 = Debate(
        round_id=round_2.id,
        prop_team_id=teams[2].id,
        opp_team_id=teams[3].id,
        room="Room 102",
    )
    session.add_all([debate_1, debate_2])
    session.flush()

    session.add_all(
        [
            DebateJudge(debate_id=debate_1.id, judge_id=judges[0].id, is_chair=True),
            DebateJudge(debate_id=debate_1.id, judge_id=judges[1].id),
            DebateJudge(debate_id=debate_1.id, judge_id=judges[2].id),
        ]
    )

    ballots = [
        Ballot(debate_id=debate_1.id, judge_id=judges[0].id, winner=Side.PROP),
        Ballot(debate_id=debate_1.id, judge_id=judges[1].id, winner=Side.PROP),
        Ballot(debate_id=debate_1.id, judge_id=judges[2].id, winner=Side.OPP),
    ]
    session.add_all(ballots)
    session.flush()

    for ballot in ballots:
        session.add_all(
            [
                SpeakerScore(
                    ballot_id=ballot.id,
                    debater_id=debaters[0].id,
                    side=Side.PROP,
                    position=SpeakerPosition.FIRST,
                    final_score=76.0,
                ),
                SpeakerScore(
                    ballot_id=ballot.id,
                    debater_id=debaters[1].id,
                    side=Side.PROP,
                    position=SpeakerPosition.SECOND,
                    final_score=74.5,
                ),
                SpeakerScore(
                    ballot_id=ballot.id,
                    debater_id=debaters[2].id,
                    side=Side.PROP,
                    position=SpeakerPosition.THIRD,
                    final_score=75.0,
                ),
                SpeakerScore(
                    ballot_id=ballot.id,
                    debater_id=debaters[0].id,
                    side=Side.PROP,
                    position=SpeakerPosition.REPLY,
                    final_score=38.0,
                ),
                SpeakerScore(
                    ballot_id=ballot.id,
                    debater_id=debaters[3].id,
                    side=Side.OPP,
                    position=SpeakerPosition.FIRST,
                    final_score=75.5,
                ),
                SpeakerScore(
                    ballot_id=ballot.id,
                    debater_id=debaters[4].id,
                    side=Side.OPP,
                    position=SpeakerPosition.SECOND,
                    final_score=75.0,
                ),
                SpeakerScore(
                    ballot_id=ballot.id,
                    debater_id=debaters[5].id,
                    side=Side.OPP,
                    position=SpeakerPosition.THIRD,
                    final_score=74.0,
                ),
                SpeakerScore(
                    ballot_id=ballot.id,
                    debater_id=debaters[4].id,
                    side=Side.OPP,
                    position=SpeakerPosition.REPLY,
                    final_score=37.5,
                ),
            ]
        )

    session.commit()
