export const languages = [
  {id:'hi',native:'हिन्दी',name:'Hindi',region:'Hindi-speaking regions',greeting:'नमस्ते, मैं आपकी कैसे मदद कर सकता हूँ?'},
  {id:'ta',native:'தமிழ்',name:'Tamil',region:'Tamil Nadu',greeting:'வணக்கம்! நான் உங்களுக்கு எப்படி உதவ முடியும்?'},
  {id:'te',native:'తెలుగు',name:'Telugu',region:'Andhra Pradesh · Telangana',greeting:'నమస్కారం! నేను మీకు ఎలా సహాయం చేయగలను?'},
  {id:'bn',native:'বাংলা',name:'Bengali',region:'West Bengal · Tripura',greeting:'নমস্কার! আমি আপনাকে কীভাবে সাহায্য করতে পারি?'},
  {id:'mr',native:'मराठी',name:'Marathi',region:'Maharashtra',greeting:'नमस्कार! मी तुम्हाला कशी मदत करू शकतो?'},
  {id:'gu',native:'ગુજરાતી',name:'Gujarati',region:'Gujarat',greeting:'નમસ્તે! હું તમને કેવી રીતે મદદ કરી શકું?'},
  {id:'kn',native:'ಕನ್ನಡ',name:'Kannada',region:'Karnataka',greeting:'ನಮಸ್ಕಾರ! ನಾನು ನಿಮಗೆ ಹೇಗೆ ಸಹಾಯ ಮಾಡಬಹುದು?'},
  {id:'ml',native:'മലയാളം',name:'Malayalam',region:'Kerala',greeting:'നമസ്കാരം! എനിക്ക് നിങ്ങളെ എങ്ങനെ സഹായിക്കാനാകും?'},
  {id:'pa',native:'ਪੰਜਾਬੀ',name:'Punjabi',region:'Punjab',greeting:'ਸਤ ਸ੍ਰੀ ਅਕਾਲ! ਮੈਂ ਤੁਹਾਡੀ ਕਿਵੇਂ ਮਦਦ ਕਰ ਸਕਦਾ ਹਾਂ?'},
  {id:'or',native:'ଓଡ଼ିଆ',name:'Odia',region:'Odisha',greeting:'ନମସ୍କାର! ମୁଁ ଆପଣଙ୍କୁ କିପରି ସାହାଯ୍ୟ କରିପାରିବି?'}
];

export function makeRng(seed = 88) {
  let value = Number(seed) >>> 0;
  return () => {
    value = (Math.imul(1664525, value) + 1013904223) >>> 0;
    return value / 4294967296;
  };
}

export function roundPetal(rand, baseR, len, width) {
  const jitter = (value, spread = .12) => value * (1 + (rand() - .5) * spread * 2);
  const base = jitter(baseR, .06), tip = base + jitter(len), w = jitter(width, .16);
  return `M ${-w * .19} ${-base} C ${-w} ${-base - len * .35}, ${-w * .75} ${-tip}, 0 ${-tip} C ${w * .75} ${-tip}, ${w} ${-base - len * .35}, ${w * .19} ${-base} C ${w * .1} ${-base + 5}, ${-w * .1} ${-base + 5}, ${-w * .19} ${-base} Z`;
}

let lotusId = 0;
export function paintedLotus({size = 96, seed = 88, spin = false, mode = ''} = {}) {
  const id = `lotus-${++lotusId}`, rand = makeRng(seed);
  const safeSize = Math.max(8, Math.min(600, Number(size) || 96));
  const layers = [{n:12,r:49,len:81,w:34,offset:0,fill:'pDeep'}, {n:12,r:35,len:65,w:29,offset:15,fill:'pMid'}, {n:8,r:20,len:53,w:26,offset:4,fill:'pSoft'}, {n:8,r:7,len:37,w:20,offset:26,fill:'pMid'}];
  const petals = layers.map(layer => Array.from({length:layer.n}, (_, i) => `<path d="${roundPetal(rand,layer.r,layer.len,layer.w)}" transform="translate(160 160) rotate(${i * 360 / layer.n + layer.offset})" fill="url(#${id}-${layer.fill})" stroke="#f0e9dd" stroke-width="1.3" stroke-opacity=".8"/>`).join('')).join('');
  return `<svg class="painted-lotus ${spin ? 'fast' : ''} ${mode === 'breathe' ? 'breathing' : ''}" width="${safeSize}" height="${safeSize}" viewBox="0 0 320 320" aria-hidden="true" focusable="false"><defs><radialGradient id="${id}-pDeep"><stop stop-color="#8c3d1f"/><stop offset="1" stop-color="#b5542e"/></radialGradient><radialGradient id="${id}-pMid"><stop stop-color="#b5542e"/><stop offset="1" stop-color="#cf7a52"/></radialGradient><radialGradient id="${id}-pSoft"><stop stop-color="#cf7a52"/><stop offset="1" stop-color="#e0a17e"/></radialGradient><filter id="${id}-brush" x="-20%" y="-20%" width="140%" height="140%"><feTurbulence type="fractalNoise" baseFrequency=".045" numOctaves="2" seed="${Number(seed) >>> 0}" result="noise"/><feDisplacementMap in="SourceGraphic" in2="noise" scale="2.3" xChannelSelector="R" yChannelSelector="G"/></filter><filter id="${id}-wash"><feGaussianBlur stdDeviation="9"/></filter></defs><circle cx="160" cy="164" r="111" fill="#b5542e" opacity=".13" filter="url(#${id}-wash)"/><g class="lotus-petals" ${safeSize >= 64 ? `filter="url(#${id}-brush)"` : ''}>${petals}<circle cx="160" cy="160" r="10" fill="#8c3d1f"/><circle cx="158" cy="158" r="2.7" fill="#f0e9dd"/></g></svg>`;
}

const entries = {
  pacs: {text:'PACS stands for Primary Agricultural Credit Society. Think of it as a local cooperative that can help members access agricultural credit and related services. What is available depends on your society. Ask its office for the current membership rules, by-laws, and service list.', tag:'COOPERATIVE BASICS',title:'Start at your local PACS',description:'Bring your question to the society office and ask for its current, written requirements before making a commitment.',metrics:[['Next step','Ask for service list'],['Rules depend on','Your society & state']]},
  insurance: {text:'PMFBY is a crop insurance scheme. Coverage, notified crops, enrolment dates, and loss-reporting procedures depend on the season and location. This demo cannot verify your eligibility or a deadline. For an actual claim, check current instructions with your insurer or the official PMFBY portal promptly.',tag:'CROP INSURANCE',title:'The right details come first',description:'For tailored guidance, a live assistant would ask for your state, district, crop, season, and whether you are enrolling or reporting a loss.',metrics:[['Eligibility','Not verified'],['Deadlines','Check current notice']]},
  grievance: {text:'For a cooperative service complaint, keep a copy of what happened, relevant dates, and any acknowledgement or receipt. Ask your society for its grievance procedure. The right escalation route depends on whether it is a state-registered or multi-state cooperative; this demo cannot determine that for you.',tag:'MEMBER SUPPORT',title:'Make the next step clear',description:'Avoid sharing account numbers or identity documents in this prototype. A live service should identify the responsible official channel before directing your complaint.',metrics:[['Useful detail','Registration type'],['Next step','Request procedure']]},
  finance: {text:'Before taking a loan, compare the interest rate, fees, repayment schedule, and total amount repayable—not just the monthly instalment. Ask the lender to explain the written terms in your language. This is a general literacy example, not a recommendation for a financial product.',tag:'FINANCIAL LITERACY',title:'Understand the full cost',description:'A useful question: “Can you show me the total amount I will repay, including every fee?”',metrics:[['Compare','Total repayment'],['Ask for','Written terms']]},
  welcome: {text:'I’m TARS, Astra’s cooperative companion. The goal is simple: speak in your language, get a clear explanation, and know where the information comes from. This studio demonstrates the experience; it does not yet retrieve official documents or connect to a robot.',tag:'ASTRA / TARS',title:'One mind. App or robot.',description:'Planned flow: microphone processing → Deepgram transcription → official-document retrieval → answer → Azure speech. The ESP32 body adds gestures, not a different knowledge base.',metrics:[['Conversation','Simulated'],['Robot link','Not connected']]},
  unknown: {text:'I’m not sure how to answer that in this prototype. No language model or official-document retrieval is connected yet. Try one of the example questions about PACS, crop insurance, complaints, or loan literacy. Live answers will need verified sources and your location where relevant.',tag:'HONEST BY DESIGN',title:'No guessing dressed as guidance',description:'Your message stays in this browser tab. The language picker changes greetings, but these demonstration answers are currently in English.',metrics:[['Source retrieval','Not connected'],['Response','Demo fallback']]}
};

export function selectDemoReply(prompt, humor = 25) {
  const text = String(prompt).toLowerCase();
  const key = /pmfby|insurance|crop|बीमा/.test(text) ? 'insurance'
    : /complaint|grievance|शिकायत/.test(text) ? 'grievance'
    : /loan|finance|financial|ऋण/.test(text) ? 'finance'
    : /pacs|by-law|bylaw|cooperative|सहकारी/.test(text) ? 'pacs'
    : /tars|astra|ps.?88|architecture|robot|hello|नमस्ते/.test(text) ? 'welcome' : 'unknown';
  const entry = entries[key];
  return {...entry, text:entry.text + (key === 'welcome' && humor >= 50 ? ' My paperwork enthusiasm is adjustable. My respect for your time is not.' : ''), context:['Illustrative, prewritten content selected by keyword—not live AI reasoning.', 'No official document was retrieved or verified for this answer.', 'UI language does not establish your state or legal jurisdiction.']};
}

export function formatSpecs(reply) {
  return `[DEMO — ${reply.tag}]\n${reply.title}\n${reply.description}\n${reply.metrics.map(([label,value]) => `${label}: ${value}`).join('\n')}\nNo live source verification.`;
}
