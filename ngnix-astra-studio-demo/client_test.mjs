/**
 * Verifies the browser client module against a running backend.
 * Node 22+ provides a global WebSocket, and client.mjs touches no DOM APIs,
 * so the exact code the page loads can be exercised here.
 *
 *   node client_test.mjs "What is a PACS?"
 */
import {BrainClient} from './client.mjs';
import {backend} from './dev-config.mjs';

const question = process.argv[2] ?? 'What is a PACS?';
const client = new BrainClient({url: backend.url, token: backend.token});

client.on('state', event => console.log(`  state -> ${event.state}`));
client.on('retrieval', event => console.log(`  retrieval: ${event.source_count} sources`));
client.on('voice_unavailable', () => console.log('  voice_unavailable (expected: browser speaks locally)'));

try {
  const ready = await client.connect();
  console.log(`connected: provider=${ready.provider} model=${ready.model} corpus=${ready.corpus_loaded}`);

  const answer = await client.ask(question, 'en');
  console.log(`\nmode=${answer.mode} grounded=${answer.grounded} elapsed=${answer.elapsed_ms}ms`);
  console.log(`sources=${JSON.stringify((answer.sources ?? []).map(s => s.id + ':' + s.title))}`);
  console.log(`\nANSWER:\n${answer.text}\n`);

  client.close();
  if (!answer.text?.trim()) { console.log('RESULT: FAIL (empty answer)'); process.exit(1); }
  console.log('RESULT: PASS');
  process.exit(0);
} catch (error) {
  console.log(`RESULT: FAIL (${error.message})`);
  process.exit(1);
}
