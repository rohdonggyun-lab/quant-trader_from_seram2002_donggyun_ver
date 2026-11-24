"""알림: 화면 + 날짜별 로그 파일 + 슬랙

전략마다 채널과 메시지 앞머리를 따로 둔다.
    모의투자 전략 → MOCK_SLACK_CHANNEL (예: #donggyun-quant)
    가상 전략     → PAPER_SLACK_CHANNEL (예: #tado_quant)

슬랙에는 정리된 블록 메시지(post)만 보내고, 자세한 기록은 화면과 로그 파일(log)에 남긴다.
확인이 필요한 일(warn)은 모아 두었다가 flush 때 "확인 필요" 메시지 하나로 보낸다.

.env 값
    SLACK_BOT_TOKEN        슬랙 봇 토큰 (없으면 슬랙 없이 로그만 남김)
    MOCK_SLACK_CHANNEL     모의투자 전략 알림 채널
    PAPER_SLACK_CHANNEL    가상 전략 알림 채널
"""
import os
from datetime import datetime
from pathlib import Path

from trader import slack_format as F

LOG_DIR = Path(__file__).resolve().parent.parent / 'data' / 'logs'


class Notifier:
    def __init__(self, channel_env='PAPER_SLACK_CHANNEL', label=''):
        self.channel = os.getenv(channel_env) or ''
        self.label = label
        self.prefix = f'[{label}] ' if label else ''
        self.quiet = False          # True면 슬랙으로 보내지 않는다 (점검용)
        self.issues = []            # 확인 필요 항목 (flush 때 한 번에 보냄)
        self.slack = None
        token = os.getenv('SLACK_BOT_TOKEN')
        if token and self.channel:
            try:
                from slack_sdk import WebClient
                self.slack = WebClient(token=token)
            except ImportError:
                pass

    def log(self, message):
        """화면과 로그 파일에만 남긴다."""
        line = datetime.now().strftime('[%m/%d %H:%M:%S] ') + self.prefix + str(message)
        print(line, flush=True)
        try:
            LOG_DIR.mkdir(parents=True, exist_ok=True)
            with open(LOG_DIR / (datetime.now().strftime('%Y%m%d') + '.log'), 'a', encoding='utf-8') as f:
                f.write(line + '\n')
        except OSError:
            pass

    def post(self, fallback, blocks):
        """블록 메시지를 보낸다. fallback은 알림 미리보기 글. 슬랙이 실패해도 계속 진행한다."""
        self.log(F.plain(blocks))
        if self.quiet or self.slack is None:
            return
        try:
            self.slack.chat_postMessage(channel=self.channel, text=fallback, blocks=blocks)
        except Exception as ex:
            self.log(f'(슬랙 전송 실패: {ex})')

    def send(self, message):
        """한 줄 알림 (제목 없이 글만)"""
        self.post(message, [F.text(message)])

    def warn(self, message):
        """확인이 필요한 일. 로그에 바로 남기고, 슬랙은 flush 때 모아서 보낸다."""
        self.log('주의 : ' + message)
        self.issues.append(message)

    def flush(self, footer=''):
        if not self.issues:
            return
        items = '\n'.join(f'• {m}' for m in self.issues)
        blocks = [F.header('⚠️ 확인 필요'), F.context(self.label), F.text(items)]
        if footer:
            blocks.append(F.context(footer))
        self.post(f'⚠️ 확인 필요 ({len(self.issues)}건) · {self.label}', blocks)
        self.issues = []

    def status(self):
        if self.slack is None:
            return '꺼짐 (.env에서 SLACK_BOT_TOKEN 또는 채널을 찾지 못함)'
        return f'켜짐 (채널 {self.channel})'
