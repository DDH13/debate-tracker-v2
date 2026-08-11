from fastapi import APIRouter, HTTPException, status
from sqlmodel import select

from app.api.deps import SessionDep, get_or_404
from app.models import Motion, MotionCreate, MotionPublic, Round

router = APIRouter(tags=["motions"])


@router.put("/rounds/{round_id}/motion", response_model=MotionPublic)
def upsert_motion(round_id: int, motion_in: MotionCreate, session: SessionDep) -> Motion:
    get_or_404(session, Round, round_id)
    motion = session.exec(select(Motion).where(Motion.round_id == round_id)).first()
    if motion is None:
        motion = Motion.model_validate(motion_in, update={"round_id": round_id})
    else:
        motion.sqlmodel_update(motion_in.model_dump())
    session.add(motion)
    session.commit()
    session.refresh(motion)
    return motion


@router.get("/rounds/{round_id}/motion", response_model=MotionPublic)
def get_motion(round_id: int, session: SessionDep) -> Motion:
    get_or_404(session, Round, round_id)
    motion = session.exec(select(Motion).where(Motion.round_id == round_id)).first()
    if motion is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Motion for round {round_id} not found",
        )
    return motion


@router.delete("/rounds/{round_id}/motion", status_code=status.HTTP_204_NO_CONTENT)
def delete_motion(round_id: int, session: SessionDep) -> None:
    get_or_404(session, Round, round_id)
    motion = session.exec(select(Motion).where(Motion.round_id == round_id)).first()
    if motion is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Motion for round {round_id} not found",
        )
    session.delete(motion)
    session.commit()
