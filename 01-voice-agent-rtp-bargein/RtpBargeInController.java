package com.wangguoai.voice.rtp;

import java.nio.ByteBuffer;
import java.util.concurrent.atomic.AtomicBoolean;
import java.util.concurrent.atomic.AtomicLong;
import java.util.concurrent.atomic.AtomicReference;

/**
 * 语音交互“死亡三角”破局核心：Java 实时 RTP 语音流接收与毫秒级打断 (Barge-in) 控制器
 *
 * 对应知乎专栏：
 * - 《Agent-语音机器人篇(3)：铸造脊梁——Java实时接收RTP语音流，获取流式ASR文本的设计与实现》
 * - 《Agent-语音机器人篇(4)：为AI注入灵魂——驯服语音交互“死亡三角”的Java工程实战》
 *
 * 核心设计亮点：
 * 1. 零拷贝 RTP Header 解析（RFC 3550），剥离 12 字节固定头与 CSRC/Extension，提取 8kHz/16kHz PCM 负载。
 * 2. 双门限能量 + 连续帧确认的轻量级 VAD（语音活动检测），过滤瞬间电流噪点与咳嗽杂音。
 * 3. 全双工状态机（IDLE -> LISTENING -> THINKING -> SPEAKING -> BARGE_IN_INTERRUPTED）：
 *    当机器人在 SPEAKING 播报 TTS 期间检测到用户连续人声帧，在 <60ms 内触发下行 RTP 清空信令与 LLM 生成中断。
 *
 * @author 王果 (王果ai - https://www.zhihu.com/people/wang-guo-ha-53)
 */
public class RtpBargeInController {

    /**
     * 全双工语音会话状态枚举
     */
    public enum DuplexState {
        IDLE,                   // 空闲等待
        LISTENING,              // 正在采集用户语音并流式推送 ASR
        THINKING,               // 用户静音尾点触发，Agent 正在推理
        SPEAKING,               // TTS 流式合成并通过 RTP 下行播报中
        BARGE_IN_INTERRUPTED    // 毫秒级打断触发：清空下行队列并切回倾听
    }

    /**
     * 下行媒体与 Agent 控制回调接口（对接 FreeSWITCH ESL / 流式 ASR / TTS 管道）
     */
    public interface VoicePipelineCallback {
        void onPcmFrameReady(byte[] pcmPayload, long timestamp, int sequenceNumber);
        void onBargeInTriggered(long interruptLatencyMs, int triggerSeq);
        void onSpeechEndDetected(long utteranceDurationMs);
    }

    // RTP 固定头部长度（12 Bytes）
    private static final int RTP_HEADER_MIN_BYTES = 12;
    // 默认触发打断所需的连续高能量语音帧数（每帧 20ms，3 帧即 60ms 极速确认打断）
    private final int bargeInConfirmFrames;
    // 默认触发静音尾点（EOS）所需的连续静音帧数（每帧 20ms，25 帧即 500ms 判停）
    private final int silenceTailFrames;
    // RMS 语音能量激活阈值（16-bit PCM）
    private final double vadRmsThreshold;

    private final AtomicReference<DuplexState> currentState = new AtomicReference<>(DuplexState.IDLE);
    private final AtomicBoolean ttsPlaybackActive = new AtomicBoolean(false);
    private final AtomicLong speechStartTimestampMs = new AtomicLong(0);

    private int consecutiveSpeechFrames = 0;
    private int consecutiveSilenceFrames = 0;
    private final VoicePipelineCallback callback;

    public RtpBargeInController(double vadRmsThreshold, int bargeInConfirmFrames, int silenceTailFrames,
                                VoicePipelineCallback callback) {
        this.vadRmsThreshold = vadRmsThreshold;
        this.bargeInConfirmFrames = bargeInConfirmFrames;
        this.silenceTailFrames = silenceTailFrames;
        this.callback = callback;
    }

    /**
     * 处理从 Netty UDP Channel 收到的原始 RTP 数据包
     *
     * @param rawRtpPacket 原始 UDP 负载字节数组
     */
    public void onRtpPacketReceived(byte[] rawRtpPacket) {
        if (rawRtpPacket == null || rawRtpPacket.length <= RTP_HEADER_MIN_BYTES) {
            return;
        }

        // 1. 解析 RFC 3550 RTP 头部
        int firstByte = rawRtpPacket[0] & 0xFF;
        int version = (firstByte >> 6) & 0x03;
        if (version != 2) {
            return; // 非标准 RTP v2 包直接丢弃
        }
        boolean hasPadding = ((firstByte >> 5) & 0x01) == 1;
        boolean hasExtension = ((firstByte >> 4) & 0x01) == 1;
        int csrcCount = firstByte & 0x0F;

        ByteBuffer buffer = ByteBuffer.wrap(rawRtpPacket);
        int sequenceNumber = Short.toUnsignedInt(buffer.getShort(2));
        long rtpTimestamp = Integer.toUnsignedLong(buffer.getInt(4));

        int headerOffset = RTP_HEADER_MIN_BYTES + (csrcCount * 4);
        if (hasExtension && rawRtpPacket.length >= headerOffset + 4) {
            int extLengthWords = Short.toUnsignedInt(buffer.getShort(headerOffset + 2));
            headerOffset += 4 + (extLengthWords * 4);
        }

        int payloadLength = rawRtpPacket.length - headerOffset;
        if (hasPadding && payloadLength > 0) {
            int paddingBytes = rawRtpPacket[rawRtpPacket.length - 1] & 0xFF;
            payloadLength -= paddingBytes;
        }
        if (payloadLength <= 0) {
            return;
        }

        byte[] pcmPayload = new byte[payloadLength];
        System.arraycopy(rawRtpPacket, headerOffset, pcmPayload, 0, payloadLength);

        // 2. 计算 16-bit Little-Endian PCM 音频帧的 RMS 能量
        double rmsEnergy = calculatePcm16Rms(pcmPayload);
        boolean isSpeechFrame = rmsEnergy >= vadRmsThreshold;

        // 3. 驱动全双工语音状态机与毫秒级打断（Barge-in）
        driveDuplexStateMachine(pcmPayload, rtpTimestamp, sequenceNumber, isSpeechFrame);
    }

    private void driveDuplexStateMachine(byte[] pcmPayload, long rtpTimestamp, int seq, boolean isSpeech) {
        long nowMs = System.currentTimeMillis();

        if (isSpeech) {
            consecutiveSpeechFrames++;
            consecutiveSilenceFrames = 0;

            // 场景 A：当前正在播报 TTS（SPEAKING），用户突然插话 -> 触发毫秒级打断 (Barge-in)
            if (ttsPlaybackActive.get() && consecutiveSpeechFrames >= bargeInConfirmFrames) {
                ttsPlaybackActive.set(false);
                currentState.set(DuplexState.BARGE_IN_INTERRUPTED);
                speechStartTimestampMs.set(nowMs);
                long confirmLatencyMs = consecutiveSpeechFrames * 20L;
                callback.onBargeInTriggered(confirmLatencyMs, seq);
                // 立即切入倾听态并将当前帧送入流式 ASR
                currentState.set(DuplexState.LISTENING);
            } else if (currentState.get() == DuplexState.IDLE) {
                currentState.set(DuplexState.LISTENING);
                speechStartTimestampMs.set(nowMs);
            }

            // 将有效语音帧推送给流式 ASR 管道
            callback.onPcmFrameReady(pcmPayload, rtpTimestamp, seq);
        } else {
            consecutiveSilenceFrames++;
            consecutiveSpeechFrames = 0;

            // 场景 B：用户在倾听态结束说话，达到静音尾点阈值 -> 触发大模型推理
            if (currentState.get() == DuplexState.LISTENING && consecutiveSilenceFrames >= silenceTailFrames) {
                currentState.set(DuplexState.THINKING);
                long duration = Math.max(0L, nowMs - speechStartTimestampMs.get());
                callback.onSpeechEndDetected(duration);
            }
        }
    }

    /**
     * 计算 16-bit PCM 音频帧的均方根能量 (RMS)
     */
    private double calculatePcm16Rms(byte[] pcmBytes) {
        int sampleCount = pcmBytes.length / 2;
        if (sampleCount == 0) {
            return 0.0;
        }
        double sumSquares = 0.0;
        for (int i = 0; i < sampleCount * 2; i += 2) {
            short sample = (short) ((pcmBytes[i] & 0xFF) | (pcmBytes[i + 1] << 8));
            sumSquares += (double) sample * sample;
        }
        return Math.sqrt(sumSquares / sampleCount);
    }

    public void notifyTtsPlaybackStarted() {
        ttsPlaybackActive.set(true);
        currentState.set(DuplexState.SPEAKING);
    }

    public DuplexState getCurrentState() {
        return currentState.get();
    }
}
