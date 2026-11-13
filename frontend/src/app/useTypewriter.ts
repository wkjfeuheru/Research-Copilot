/**
 * 打字机流式输出 Hook：把目标文本按节奏逐字显示，避免大段内容一次性砸出。
 * 只控制展示层文本，不影响调用方持有的原始数据与校验逻辑。
 */

import { useEffect, useReducer, useRef } from "react";

export interface TypewriterOptions {
  enabled?: boolean;
  tickMs?: number;
  maxDurationMs?: number;
}

export interface TypewriterState {
  displayed: string;
  typing: boolean;
}

export function useTypewriter(
  text: string,
  options?: TypewriterOptions,
): TypewriterState {
  const { enabled = true, tickMs = 24, maxDurationMs = 8000 } = options ?? {};

  const [, rerender] = useReducer((count: number) => count + 1, 0);
  const positionRef = useRef(0); // 已显示的字符数，追加场景下跨渲染保留
  const targetRef = useRef(""); // 上一轮目标文本，用于区分"追加"与"整体替换"

  // 命中系统"减少动态效果"偏好时跳过动画，尊重无障碍设置
  const reducedMotion =
    typeof window !== "undefined" &&
    window.matchMedia("(prefers-reduced-motion: reduce)").matches;

  const active = enabled && text.length > 0 && !reducedMotion;

  // 在渲染期同步决策，避免新文本先闪现一帧旧进度；
  // 目标被整体替换（新消息）时从头播放，仅追加（流式分片）时沿用现有进度
  if (!active) {
    positionRef.current = text.length;
    targetRef.current = text;
  } else if (!text.startsWith(targetRef.current)) {
    positionRef.current = 0;
    targetRef.current = text;
  } else {
    targetRef.current = text;
  }

  const typing = active && positionRef.current < text.length;

  useEffect(() => {
    if (!typing) return;
    // 自适应速度：按当前长度反推每 tick 步长，保证整段动画不超过 maxDurationMs；
    // 文本流式增长时步长随之加大，避免越写越追不上。短文本保底每 tick 1-2 字。
    const charsPerTick = Math.max(
      text.length <= 40 ? 2 : 1,
      Math.ceil((text.length * tickMs) / maxDurationMs),
    );
    const timer = window.setInterval(() => {
      positionRef.current = Math.min(
        positionRef.current + charsPerTick,
        text.length,
      );
      rerender();
      if (positionRef.current >= text.length) window.clearInterval(timer);
    }, tickMs);
    return () => window.clearInterval(timer);
  }, [typing, text, tickMs, maxDurationMs]);

  return {
    displayed: active ? text.slice(0, positionRef.current) : text,
    typing,
  };
}
