export interface PlaceholderSkill {
  id: string;
  name: string;
  description: string;
}

/**
 * 仅用于展示技能选择交互。阶段一没有技能目录，不能加载或执行这些技能。
 */
export const PLACEHOLDER_SKILLS: readonly PlaceholderSkill[] = [
  {
    id: 'financial-analysis',
    name: '财务分析',
    description: '对齐报告期与指标口径，检查增长质量（占位）。',
  },
  {
    id: 'industry-comparison',
    name: '行业对比',
    description: '比较代表公司的经营差异与风险（占位）。',
  },
  {
    id: 'financial-charts',
    name: '财务图表',
    description: '把计算结果组织为可核对的简报图表（占位）。',
  },
];

const skillNamesById = new Map(
  PLACEHOLDER_SKILLS.map((skill) => [skill.id, skill.name]),
);

export function getSkillNames(ids: readonly string[]): string[] {
  return ids.flatMap((id) => {
    const name = skillNamesById.get(id);
    return name ? [name] : [];
  });
}
