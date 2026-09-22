#!/usr/bin/env node
// Idempotent demo data: organizations, staff accounts and a question bank
// large enough to compose a 20-question paper with >= 30% difficulty >= 4
// (32 questions: 12 hard, 20 easier, covering all three question types).
const {
  sequelize,
  Organization,
  User,
  Question,
} = require('../models');
const paperService = require('../services/paperService');

const QUESTIONS = [
  // ---------- difficulty 5 (hard) ----------
  { type: 'multiple', difficulty: 5, stem: '抢救室内一名患者突发室颤，以下哪些措施必须立即执行？', options: ['呼叫应急团队并开始CPR', '立即双向波200J除颤', '等待家属签署知情同意', '除颤后立即恢复胸外按压'], answer: ['A', 'B', 'D'] },
  { type: 'multiple', difficulty: 5, stem: '关于脓毒症集束化治疗（1小时内），下列哪些正确？', options: ['留取血培养后再用抗菌药', '测量乳酸', '低血压时3小时内补液即可', '启动血管活性药维持MAP≥65mmHg'], answer: ['A', 'B', 'D'] },
  { type: 'single', difficulty: 5, stem: '患者呼气末二氧化碳在CPR中突然持续升高，最可能提示？', options: ['胸外按压质量恶化', '自主循环恢复(ROSC)', '气管插管脱出', '肺栓塞'], answer: ['B'] },
  { type: 'boolean', difficulty: 5, stem: '对服用新型口服抗凝药的颅内出血患者，可一律按相同半衰期统一使用一种逆转剂。', options: ['true', 'false'], answer: ['false'] },
  { type: 'multiple', difficulty: 4, stem: '识别成人心脏骤停的依据包括？', options: ['无反应', '无正常呼吸或仅濒死叹息样呼吸', '必须先触诊确认无脉搏满30秒', '立即启动急救系统'], answer: ['A', 'B', 'D'] },
  { type: 'single', difficulty: 4, stem: '过敏性休克首选药物与给药途径是？', options: ['地塞米松静脉推注', '肾上腺素大腿前外侧肌注', '苯海拉明口服', '沙丁胺醇雾化'], answer: ['B'] },
  { type: 'multiple', difficulty: 4, stem: '以下哪些是导管相关血流感染的预防要点？', options: ['手卫生', '每日评估留置必要性', '穿刺部位常规涂抗生素软膏', '最大无菌屏障置管'], answer: ['A', 'B', 'D'] },
  { type: 'single', difficulty: 4, stem: '成人CPR按压深度与频率正确的是？', options: ['3-4cm，60-80次/分', '5-6cm，100-120次/分', '至少7cm，140次/分', '4-5cm，80-100次/分'], answer: ['B'] },
  { type: 'boolean', difficulty: 4, stem: '对不稳定的室性心动过速患者，同步电复律应在镇静条件下尽快进行。', options: ['true', 'false'], answer: ['true'] },
  { type: 'multiple', difficulty: 4, stem: '给药“三查七对”中，至少应核对哪些内容？', options: ['患者身份', '药名与剂量', '患者职业', '给药途径与时间'], answer: ['A', 'B', 'D'] },
  { type: 'single', difficulty: 4, stem: '血氧饱和度低于多少且持续提示需要立即评估缺氧（一般成人）？', options: ['95%', '92%', '88%', '80%'], answer: ['B'] },
  { type: 'boolean', difficulty: 4, stem: '发现患者输液发生急性肺水肿表现时，应减慢/停止输液并取端坐位、通知医生。', options: ['true', 'false'], answer: ['true'] },

  // ---------- difficulty 1-3 ----------
  { type: 'single', difficulty: 1, stem: '成人正常腋温范围约为？', options: ['36.0-37.0℃', '37.5-38.5℃', '35.0-35.5℃', '38.0-39.0℃'], answer: ['A'] },
  { type: 'single', difficulty: 1, stem: '测量血压时袖带应位于？', options: ['肘窝上方约2-3cm', '手腕处', '肘窝正中', '前臂下1/3'], answer: ['A'] },
  { type: 'boolean', difficulty: 1, stem: '洗手应使用流动水并按七步洗手法揉搓足够时间。', options: ['true', 'false'], answer: ['true'] },
  { type: 'boolean', difficulty: 1, stem: '医疗废物可以与生活垃圾混合丢弃。', options: ['true', 'false'], answer: ['false'] },
  { type: 'single', difficulty: 2, stem: '成人心肺复苏按压与通气比（未建立高级气道）为？', options: ['15:2', '30:2', '5:1', '持续按压不通气'], answer: ['B'] },
  { type: 'single', difficulty: 2, stem: '低血糖的诊断阈值通常为血糖低于？', options: ['3.9 mmol/L', '5.6 mmol/L', '2.0 mmol/L', '7.0 mmol/L'], answer: ['A'] },
  { type: 'multiple', difficulty: 2, stem: '下列哪些属于生命体征？', options: ['体温', '脉搏', '血型', '呼吸频率'], answer: ['A', 'B', 'D'] },
  { type: 'boolean', difficulty: 2, stem: '鼻饲前应确认胃管位置并回抽胃液。', options: ['true', 'false'], answer: ['true'] },
  { type: 'single', difficulty: 2, stem: '标准预防措施假定谁的血液体液均有传染性？', options: ['仅确诊传染病患者', '所有人', '仅发热患者', '仅住院患者'], answer: ['B'] },
  { type: 'single', difficulty: 3, stem: '药物过敏休克早期最常见的皮肤表现是？', options: ['黄疸', '荨麻疹/血管性水肿', '瘀斑', '色素沉着'], answer: ['B'] },
  { type: 'multiple', difficulty: 3, stem: '跌倒高危患者入院后应采取哪些措施？', options: ['跌倒风险标识', '保持床栏与呼叫器在位', '约束带全天候捆绑', '夜间留灯、地面干燥'], answer: ['A', 'B', 'D'] },
  { type: 'single', difficulty: 3, stem: '成人外周静脉补钾时，1000ml液体中氯化钾一般不超过？', options: ['0.3%（约3g）', '1%', '5%', '10%'], answer: ['A'] },
  { type: 'boolean', difficulty: 3, stem: '发现患者噎食且无法咳嗽、发声时，应立即采用海姆立克急救法。', options: ['true', 'false'], answer: ['true'] },
  { type: 'single', difficulty: 3, stem: '护理记录应遵循的原则不包括？', options: ['客观', '及时', '凭推测补记', '准确'], answer: ['C'] },
  { type: 'multiple', difficulty: 3, stem: '口服药发放前需确认？', options: ['患者床号姓名', '药物名称剂量', '患者当日饮食喜好', '服药时间与用法'], answer: ['A', 'B', 'D'] },
  { type: 'boolean', difficulty: 2, stem: '无菌操作时取出的无菌物品即使未使用也不可放回无菌容器。', options: ['true', 'false'], answer: ['true'] },
  { type: 'single', difficulty: 2, stem: '正常人空腹血糖参考范围约为？', options: ['1.0-2.0 mmol/L', '3.9-6.1 mmol/L', '7.0-9.0 mmol/L', '9.0-11.0 mmol/L'], answer: ['B'] },
  { type: 'single', difficulty: 3, stem: '心电监护电极片应避开的部位是？', options: ['胸骨右缘', '心尖部', '除颤部位与伤口', '锁骨下'], answer: ['C'] },
  { type: 'boolean', difficulty: 3, stem: '使用约束带前应充分评估并取得知情同意，同时定时松解观察。', options: ['true', 'false'], answer: ['true'] },
  { type: 'multiple', difficulty: 3, stem: '手卫生的五个时刻包括？', options: ['接触患者前', '清洁/无菌操作前', '交接班前', '接触患者周围环境后'], answer: ['A', 'B', 'D'] },
  { type: 'single', difficulty: 2, stem: '体温单上脉搏通常用什么颜色绘制？', options: ['蓝色', '红色', '黑色', '绿色'], answer: ['B'] },
];

async function seed() {
  const [org, orgCreated] = await Organization.findOrCreate({
    where: { name: '华北护理示范中心' },
    defaults: { timezone: 'Asia/Shanghai' },
  });
  const [org2] = await Organization.findOrCreate({
    where: { name: '华东康复医院' },
    defaults: { timezone: 'Asia/Shanghai' },
  });

  async function ensureUser(name, role, organizationId) {
    const [u] = await User.findOrCreate({ where: { name, organizationId }, defaults: { role } });
    return u;
  }

  const admin = await ensureUser('平台管理员', 'admin', null);
  await ensureUser('王主管', 'supervisor', org.id);
  await ensureUser('李主管', 'supervisor', org2.id);
  await ensureUser('张三', 'student', org.id);
  await ensureUser('李四', 'student', org.id);
  await ensureUser('王五', 'student', org2.id);

  const count = await Question.count();
  if (count === 0) {
    await Question.bulkCreate(
      QUESTIONS.map((q) => ({ ...q, explanation: '见护理操作规程与急救指南。', organizationId: null, active: true }))
    );
  }

  // A deterministic paper so demo consumers always see the same question set.
  const paper = await paperService.generatePaper({
    organizationId: org.id,
    title: 'CareOps 员工能力考核演示卷',
    seed: 'careops-demo-seed-v1',
  });

  console.log(JSON.stringify({
    organizations: { demo: org.id, other: org2.id },
    users: {
      admin: admin.id,
      supervisorOrg1: (await User.findOne({ where: { name: '王主管' } })).id,
      supervisorOrg2: (await User.findOne({ where: { name: '李主管' } })).id,
      zhangsan: (await User.findOne({ where: { name: '张三' } })).id,
      lisi: (await User.findOne({ where: { name: '李四' } })).id,
      wangwu: (await User.findOne({ where: { name: '王五' } })).id,
    },
    paperId: paper.id,
    questions: count === 0 ? QUESTIONS.length : count,
  }, null, 2));
}

if (require.main === module) {
  seed()
    .then(() => sequelize.close())
    .then(() => process.exit(0))
    .catch((err) => {
      console.error('seed failed:', err);
      process.exit(1);
    });
}

module.exports = { seed, QUESTIONS };
