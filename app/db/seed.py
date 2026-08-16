from datetime import date

from sqlmodel import Session

from app.models import (
    Ballot,
    BPBallot,
    BPBallotTeam,
    BPDebate,
    BPDebateJudge,
    BPDebateTeam,
    BPPosition,
    BPSide,
    BPSpeakerScore,
    Debate,
    DebateFormat,
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
        Debater(
            full_name="Alice Adams",
            first_name="Alice",
            last_name="Adams",
            email="alice@uni-a.edu",
            institution_id=institutions[0].id,
        ),
        Debater(full_name="Andrew Ali", first_name="Andrew", last_name="Ali", institution_id=institutions[0].id),
        Debater(full_name="Amy Ito", first_name="Amy", last_name="Ito", institution_id=institutions[0].id),
        Debater(
            full_name="Bob Brooks",
            first_name="Bob",
            last_name="Brooks",
            email="bob@uni-b.edu",
            institution_id=institutions[1].id,
        ),
        Debater(full_name="Bella Byrne", first_name="Bella", last_name="Byrne", institution_id=institutions[1].id),
        Debater(full_name="Ben Barnes", first_name="Ben", last_name="Barnes", institution_id=institutions[1].id),
        Debater(full_name="Cara Chen", first_name="Cara", last_name="Chen", institution_id=institutions[2].id),
        Debater(full_name="Carl Cole", first_name="Carl", last_name="Cole", institution_id=institutions[2].id),
        Debater(full_name="Cathy Cruz", first_name="Cathy", last_name="Cruz", institution_id=institutions[2].id),
        Debater(full_name="Dana Diaz", first_name="Dana", last_name="Diaz", institution_id=institutions[3].id),
        Debater(full_name="Dave Dunn", first_name="Dave", last_name="Dunn", institution_id=institutions[3].id),
        Debater(
            full_name="Sam Sharma",
            first_name="Sam",
            last_name="Sharma",
            email="sam.sharma@example.com",
            institution_id=institutions[3].id,
        ),
    ]
    session.add_all(debaters)

    judges = [
        Judge(
            full_name="Jordan Park",
            first_name="Jordan",
            last_name="Park",
            email="jordan@judges.org",
            institution_id=institutions[1].id,
        ),
        Judge(full_name="Sam Sharma", first_name="Sam", last_name="Sharma", email="sam.sharma@example.com"),
        Judge(
            full_name="Priya Patel",
            first_name="Priya",
            last_name="Patel",
            email="priya@judges.org",
            institution_id=institutions[2].id,
        ),
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
                    content=30.5,
                    style=30.5,
                    strategy=15.0,
                    final_score=76.0,
                ),
                SpeakerScore(
                    ballot_id=ballot.id,
                    debater_id=debaters[1].id,
                    side=Side.PROP,
                    position=SpeakerPosition.SECOND,
                    content=30.0,
                    style=29.5,
                    strategy=15.0,
                    final_score=74.5,
                ),
                SpeakerScore(
                    ballot_id=ballot.id,
                    debater_id=debaters[2].id,
                    side=Side.PROP,
                    position=SpeakerPosition.THIRD,
                    content=30.0,
                    style=30.0,
                    strategy=15.0,
                    final_score=75.0,
                ),
                SpeakerScore(
                    ballot_id=ballot.id,
                    debater_id=debaters[0].id,
                    side=Side.PROP,
                    position=SpeakerPosition.REPLY,
                    content=15.0,
                    style=15.0,
                    strategy=8.0,
                    final_score=38.0,
                ),
                SpeakerScore(
                    ballot_id=ballot.id,
                    debater_id=debaters[3].id,
                    side=Side.OPP,
                    position=SpeakerPosition.FIRST,
                    content=30.0,
                    style=30.5,
                    strategy=15.0,
                    final_score=75.5,
                ),
                SpeakerScore(
                    ballot_id=ballot.id,
                    debater_id=debaters[4].id,
                    side=Side.OPP,
                    position=SpeakerPosition.SECOND,
                    content=30.0,
                    style=30.0,
                    strategy=15.0,
                    final_score=75.0,
                ),
                SpeakerScore(
                    ballot_id=ballot.id,
                    debater_id=debaters[5].id,
                    side=Side.OPP,
                    position=SpeakerPosition.THIRD,
                    content=29.5,
                    style=29.5,
                    strategy=15.0,
                    final_score=74.0,
                ),
                SpeakerScore(
                    ballot_id=ballot.id,
                    debater_id=debaters[4].id,
                    side=Side.OPP,
                    position=SpeakerPosition.REPLY,
                    content=15.0,
                    style=15.0,
                    strategy=7.5,
                    final_score=37.5,
                ),
            ]
        )

    _seed_bp(session)
    session.commit()


def _seed_bp(session: Session) -> None:
    tournament = Tournament(
        name="Sample BP Open", date=date(2024, 7, 1), slug="sample-bp-open", format=DebateFormat.BP
    )
    session.add(tournament)
    session.flush()

    institutions = [
        Institution(name="University E", code="UE"),
        Institution(name="University F", code="UF"),
        Institution(name="University G", code="UG"),
        Institution(name="University H", code="UH"),
    ]
    session.add_all(institutions)
    session.flush()

    teams = [
        Team(tournament_id=tournament.id, name="Team OG", institution_id=institutions[0].id),
        Team(tournament_id=tournament.id, name="Team OO", institution_id=institutions[1].id),
        Team(tournament_id=tournament.id, name="Team CG", institution_id=institutions[2].id),
        Team(tournament_id=tournament.id, name="Team CO", institution_id=institutions[3].id),
    ]
    session.add_all(teams)
    session.flush()

    debaters = [
        Debater(full_name="Erin Ellis", first_name="Erin", last_name="Ellis", institution_id=institutions[0].id),
        Debater(full_name="Evan Ellis", first_name="Evan", last_name="Ellis", institution_id=institutions[0].id),
        Debater(full_name="Fay Ford", first_name="Fay", last_name="Ford", institution_id=institutions[1].id),
        Debater(full_name="Finn Ford", first_name="Finn", last_name="Ford", institution_id=institutions[1].id),
        Debater(full_name="Gail Grant", first_name="Gail", last_name="Grant", institution_id=institutions[2].id),
        Debater(full_name="Gus Grant", first_name="Gus", last_name="Grant", institution_id=institutions[2].id),
        Debater(full_name="Hana Hill", first_name="Hana", last_name="Hill", institution_id=institutions[3].id),
        Debater(full_name="Huck Hill", first_name="Huck", last_name="Hill", institution_id=institutions[3].id),
    ]
    session.add_all(debaters)
    session.flush()

    session.add_all(
        [
            TeamMember(team_id=teams[0].id, debater_id=debaters[0].id),
            TeamMember(team_id=teams[0].id, debater_id=debaters[1].id),
            TeamMember(team_id=teams[1].id, debater_id=debaters[2].id),
            TeamMember(team_id=teams[1].id, debater_id=debaters[3].id),
            TeamMember(team_id=teams[2].id, debater_id=debaters[4].id),
            TeamMember(team_id=teams[2].id, debater_id=debaters[5].id),
            TeamMember(team_id=teams[3].id, debater_id=debaters[6].id),
            TeamMember(team_id=teams[3].id, debater_id=debaters[7].id),
        ]
    )

    judge = Judge(full_name="Ivy Iyer", first_name="Ivy", last_name="Iyer", email="ivy@judges.org")
    session.add(judge)
    session.flush()

    round_1 = Round(tournament_id=tournament.id, seq=1, name="Round 1")
    session.add(round_1)
    session.flush()

    session.add(
        Motion(
            round_id=round_1.id,
            text="This House Would prioritize climate adaptation over mitigation.",
        )
    )

    bp_debate = BPDebate(round_id=round_1.id, room="Room 201")
    session.add(bp_debate)
    session.flush()

    session.add_all(
        [
            BPDebateTeam(bp_debate_id=bp_debate.id, team_id=teams[0].id, side=BPSide.OG, rank=1, points=3),
            BPDebateTeam(bp_debate_id=bp_debate.id, team_id=teams[1].id, side=BPSide.OO, rank=4, points=0),
            BPDebateTeam(bp_debate_id=bp_debate.id, team_id=teams[2].id, side=BPSide.CG, rank=2, points=2),
            BPDebateTeam(bp_debate_id=bp_debate.id, team_id=teams[3].id, side=BPSide.CO, rank=3, points=1),
        ]
    )
    session.add(BPDebateJudge(bp_debate_id=bp_debate.id, judge_id=judge.id, is_chair=True))
    session.flush()

    ballot = BPBallot(bp_debate_id=bp_debate.id, judge_id=judge.id)
    session.add(ballot)
    session.flush()

    session.add_all(
        [
            BPBallotTeam(bp_ballot_id=ballot.id, side=BPSide.OG, rank=1),
            BPBallotTeam(bp_ballot_id=ballot.id, side=BPSide.OO, rank=4),
            BPBallotTeam(bp_ballot_id=ballot.id, side=BPSide.CG, rank=2),
            BPBallotTeam(bp_ballot_id=ballot.id, side=BPSide.CO, rank=3),
        ]
    )

    session.add_all(
        [
            BPSpeakerScore(
                bp_ballot_id=ballot.id,
                debater_id=debaters[0].id,
                side=BPSide.OG,
                position=BPPosition.FIRST,
                final_score=76.0,
            ),
            BPSpeakerScore(
                bp_ballot_id=ballot.id,
                debater_id=debaters[1].id,
                side=BPSide.OG,
                position=BPPosition.SECOND,
                final_score=75.0,
            ),
            BPSpeakerScore(
                bp_ballot_id=ballot.id,
                debater_id=debaters[2].id,
                side=BPSide.OO,
                position=BPPosition.FIRST,
                final_score=70.0,
            ),
            BPSpeakerScore(
                bp_ballot_id=ballot.id,
                debater_id=debaters[3].id,
                side=BPSide.OO,
                position=BPPosition.SECOND,
                final_score=69.5,
            ),
            BPSpeakerScore(
                bp_ballot_id=ballot.id,
                debater_id=debaters[4].id,
                side=BPSide.CG,
                position=BPPosition.FIRST,
                final_score=74.0,
            ),
            BPSpeakerScore(
                bp_ballot_id=ballot.id,
                debater_id=debaters[5].id,
                side=BPSide.CG,
                position=BPPosition.SECOND,
                final_score=73.5,
            ),
            BPSpeakerScore(
                bp_ballot_id=ballot.id,
                debater_id=debaters[6].id,
                side=BPSide.CO,
                position=BPPosition.FIRST,
                final_score=72.0,
            ),
            BPSpeakerScore(
                bp_ballot_id=ballot.id,
                debater_id=debaters[7].id,
                side=BPSide.CO,
                position=BPPosition.SECOND,
                final_score=71.5,
            ),
        ]
    )
