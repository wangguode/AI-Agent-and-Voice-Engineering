#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Voice Agent 全双工流式管道与毫秒级打断 (Barge-in) 仿真引擎
=========================================================
对应知乎 @王果ai 专栏：
- 《Agent-语音机器人篇(4)：为 AI 注入灵魂——驯服语音交互“死亡三角”的工程实战》
- 《Agent-语音机器人篇(5)：Voice Agent 全栈架构深度解析》

设计融合与脱敏自高 Star 实时语音框架的核心异步帧管道 (Frame Pipeline) 思想：
1. 统一将音频流、VAD 事件、ASR 增量文本、LLM Token 流与控制信令抽象为 Frame。
2. 当下行 TTS 正在播报音频帧时，一旦上行 VAD 连续检测到 3 帧（60ms）有效人声，
   立即向全链路广播 CancelFrame（打断信号），在毫秒级内清空 TTS 缓冲区并中断 LLM 生成。

运行方式：
    python3 bargein_voice_engine.py
"""

from __future__ import annotations
import asyncio
import math
import struct
import time
from dataclasses import dataclass, field
from enum import Enum, auto
from typing import AsyncIterator, List


class DuplexVoiceState(Enum):
    IDLE = auto()
    LISTENING = auto()
    THINKING = auto()
    SPEAKING = auto()
    BARGE_IN = auto()


@dataclass
class AudioRtpFrame:
    """20ms RTP 音频帧 (16kHz, 16-bit Mono PCM = 640 bytes)"""
    seq: int
    timestamp_ms: float
    pcm_bytes: bytes
    rms_energy: float = field(init=False)

    def __post_init__(self) -> None:
        sample_count = len(self.pcm_bytes) // 2
        if sample_count == 0:
            self.rms_energy = 0.0
            return
        samples = struct.unpack(f"<{sample_count}h", self.pcm_bytes)
        self.rms_energy = math.sqrt(sum(s * s for s in samples) / sample_count)


class FullDuplexVoiceAgent:
    """
    驯服语音交互“死亡三角”（高并发、超低延迟、自然打断）的全双工控制器
    """

    def __init__(
        self,
        vad_rms_threshold: float = 1200.0,
        barge_in_confirm_frames: int = 3,  # 3 * 20ms = 60ms 极速打断确认
        eos_silence_frames: int = 15,      # 15 * 20ms = 300ms 静音尾点判停
    ) -> None:
        self.vad_rms_threshold = vad_rms_threshold
        self.barge_in_confirm_frames = barge_in_confirm_frames
        self.eos_silence_frames = eos_silence_frames

        self.state: DuplexVoiceState = DuplexVoiceState.IDLE
        self.consecutive_speech: int = 0
        self.consecutive_silence: int = 0
        self.tts_cancel_event = asyncio.Event()
        self.metrics_log: List[str] = []

    async def ingest_rtp_stream(self, frames: AsyncIterator[AudioRtpFrame]) -> None:
        """上行 RTP 音频流实时处理主循环"""
        utterance_buffer: List[int] = []
        tts_task: asyncio.Task | None = None

        async for frame in frames:
            is_speech = frame.rms_energy >= self.vad_rms_threshold

            if is_speech:
                self.consecutive_speech += 1
                self.consecutive_silence = 0

                # 核心：在机器人播报期间检测到用户连续说话帧 -> 毫秒级 Barge-in 打断
                if (
                    self.state == DuplexVoiceState.SPEAKING
                    and self.consecutive_speech >= self.barge_in_confirm_frames
                ):
                    barge_in_latency_ms = self.consecutive_speech * 20
                    self.state = DuplexVoiceState.BARGE_IN
                    self.tts_cancel_event.set()
                    msg = (
                        f"⚡ [BARGE-IN 毫秒级打断触发] Seq={frame.seq} | "
                        f"确认耗时={barge_in_latency_ms}ms | 已清空下行 TTS 队列并中止 LLM 流"
                    )
                    print(msg)
                    self.metrics_log.append(msg)
                    if tts_task and not tts_task.done():
                        tts_task.cancel()
                    self.state = DuplexVoiceState.LISTENING
                    utterance_buffer.clear()

                elif self.state == DuplexVoiceState.IDLE:
                    self.state = DuplexVoiceState.LISTENING
                    print(f"🎙️ [VAD 语音起点] Seq={frame.seq} (RMS={frame.rms_energy:.0f}) -> 开始流式 ASR")

                utterance_buffer.append(frame.seq)

            else:
                self.consecutive_silence += 1
                self.consecutive_speech = 0

                if (
                    self.state == DuplexVoiceState.LISTENING
                    and self.consecutive_silence >= self.eos_silence_frames
                ):
                    self.state = DuplexVoiceState.THINKING
                    eos_ms = self.consecutive_silence * 20
                    print(
                        f"🧠 [VAD 静音尾点判停] 采集帧数={len(utterance_buffer)} | "
                        f"尾点检测={eos_ms}ms -> 触发流式 LLM + TTS"
                    )
                    utterance_buffer.clear()
                    self.tts_cancel_event.clear()
                    tts_task = asyncio.create_task(self._stream_llm_and_tts())

            await asyncio.sleep(0.002)

    async def _stream_llm_and_tts(self) -> None:
        """模拟流式 LLM 首包生成与流式 TTS 下行 RTP 推流"""
        t0 = time.perf_counter()
        await asyncio.sleep(0.015)  # 模拟极速首包 TTFB
        ttfb_ms = (time.perf_counter() - t0) * 1000
        self.state = DuplexVoiceState.SPEAKING
        print(f"🔊 [TTS 下行首包播报] 端到端首包延迟 TTFB={ttfb_ms:.1f}ms | 正在播报回复...")

        try:
            for chunk_idx in range(1, 15):
                if self.tts_cancel_event.is_set():
                    return
                await asyncio.sleep(0.01)
            self.state = DuplexVoiceState.IDLE
            print("✅ [TTS 播报自然结束] 会话回到 IDLE 倾听等待态")
        except asyncio.CancelledError:
            pass


def make_pcm_frame(seq: int, amplitude: int) -> AudioRtpFrame:
    """生成指定振幅的 20ms 16kHz 16-bit PCM 仿真帧"""
    pcm = struct.pack("<320h", *([amplitude] * 320))
    return AudioRtpFrame(seq=seq, timestamp_ms=seq * 20.0, pcm_bytes=pcm)


async def simulate_conversation() -> None:
    print("=" * 72)
    print("🚀 启动 Voice Agent 全双工 RTP 流式管道与 Barge-in 打断压测")
    print("=" * 72)
    agent = FullDuplexVoiceAgent()

    async def mock_rtp_generator() -> AsyncIterator[AudioRtpFrame]:
        seq = 1
        # 第 1 阶段：用户说第一句话（10 帧语音 = 200ms）
        for _ in range(10):
            yield make_pcm_frame(seq, amplitude=3500)
            seq += 1
        # 第 2 阶段：用户停顿（16 帧静音 = 320ms）触发 Agent 思考并开始 TTS 播报
        for _ in range(16):
            yield make_pcm_frame(seq, amplitude=80)
            seq += 1
        # 等待 TTS 进入 SPEAKING 播报中
        await asyncio.sleep(0.04)
        # 第 3 阶段：机器人在播报中途，用户突然插话（连续 5 帧高能量语音）-> 触发 60ms 极速打断！
        for _ in range(5):
            yield make_pcm_frame(seq, amplitude=4200)
            seq += 1
        # 第 4 阶段：用户插话完毕停顿（16 帧静音）-> Agent 针对新问题完成完整播报
        for _ in range(16):
            yield make_pcm_frame(seq, amplitude=50)
            seq += 1
        await asyncio.sleep(0.2)

    await agent.ingest_rtp_stream(mock_rtp_generator())
    print("=" * 72)


if __name__ == "__main__":
    asyncio.run(simulate_conversation())
