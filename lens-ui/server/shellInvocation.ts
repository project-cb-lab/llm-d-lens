type Word = { value: string; start: number; end: number };

// Parse, never execute, a single shell invocation. Keep spans so untouched
// arguments retain their quoting and expansion semantics.
export function shellWords(script: string): Word[] {
    const words: Word[] = [];
    let index = 0;
    while (index < script.length) {
        if (/\s/.test(script[index])) {
            if (script[index] === '\n' && words.length && script.slice(index).trim()) throw new Error('Model-server configuration requires a single shell command.');
            index++; continue;
        }
        if (script[index] === '\\' && script[index + 1] === '\n') { index += 2; continue; }
        const start = index;
        let value = '';
        let quote = '';
        while (index < script.length) {
            const character = script[index];
            if (!quote && /\s/.test(character)) break;
            if (!quote && /[;|&<>#`()]/.test(character)) throw new Error('Model-server configuration requires a single vllm serve invocation.');
            if (character === quote) { quote = ''; index++; continue; }
            if (!quote && (character === "'" || character === '"')) { quote = character; index++; continue; }
            if (character === '\\' && quote !== "'") {
                if (script[index + 1] === '\n') { index += 2; continue; }
                if (index + 1 >= script.length) throw new Error('Incomplete shell escape in model-server command.');
                value += script[index + 1]; index += 2; continue;
            }
            value += character; index++;
        }
        if (quote) throw new Error('Unclosed quote in model-server command.');
        words.push({ value, start, end: index });
    }
    return words;
}
