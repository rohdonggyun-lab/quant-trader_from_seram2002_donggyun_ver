"""알림: 화면 + 날짜별 로그 파일 + 슬랙(가상 매매 전용 채널)

.env 값
    SLACK_BOT_TOKEN        슬랙 봇 토큰 (없으면 슬랙 없이 로그만 남김)
    PAPER_SLACK_CHANNEL    가상 매매 알림 채널 (예: #paper-trading)
"""
import os
from datetime import datetime
from pathlib import Path

LOG_DIR = Path(__file__).resolve().parent.parent / 'data' / 'logs'
PREFIX = '[가상] '


class Notifier:
    def __init__(self):
        token = os.getenv('SLACK_BOT_TOKEN')
        self.channel = os.getenv('PAPER_SLACK_CHANNEL') or '#paper-trading'
        self.slack = None
        if token:
            try:
                from slack_sdk import WebClient
                self.slack = WebClient(token=token)
            except ImportError:
                pass

    def log(self, message):
        """화면과 로그 파일에만 남긴다."""
        line = datetime.now().strftime('[%m/%d %H:%M:%S] ') + str(message)
        print(line, flush=True)
        try:
            LOG_DIR.mkdir(parents=True, exist_ok=True)
            with open(LOG_DIR / (datetime.now().strftime('%Y%m%d') + '.log'),
                      'a', encoding='utf-8') as f:
                f.write(line + '\n')
        except OSError:
            pass

    def send(self, message):
        """화면, 로그 파일, 슬랙에 모두 남긴다. 슬랙이 실패해도 계속 진행한다."""
        self.log(message)
        if self.slack is None:
            return
        try:
            self.slack.chat_postMessage(
                channel=self.channel,
                text=PREFIX + datetime.now().strftime('[%m/%d %H:%M:%S] ') + message)
        except Exception as ex:
            self.log(f'(슬랙 전송 실패: {ex})')

    def status(self):
        return (f'켜짐 (채널 {self.channel})' if self.slack is not None
                else '꺼짐 (.env에서 SLACK_BOT_TOKEN을 찾지 못함)')
