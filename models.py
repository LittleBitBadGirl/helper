from sqlalchemy import Column, Integer, String, DateTime, Boolean, Text, ForeignKey
from database import Base
from datetime import datetime

class TaskList(Base):
    __tablename__ = "task_lists"

    id = Column(Integer, primary_key=True)
    message_id = Column(Integer, index=True)
    chat_id = Column(Integer)
    user_id = Column(Integer)
    user_full_name = Column(String)
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)
    raw_text = Column(Text)
    total_tasks = Column(Integer, default=0)
    completed_tasks = Column(Integer, default=0)

class TaskItem(Base):
    __tablename__ = "task_items"

    id = Column(Integer, primary_key=True)
    list_id = Column(Integer, ForeignKey("task_lists.id"))
    text = Column(String)
    is_completed = Column(Boolean, default=False)
