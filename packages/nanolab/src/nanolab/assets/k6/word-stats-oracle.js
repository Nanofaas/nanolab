export function equal(actual, expected) {
    if (actual === expected) return true;
    if (actual === null || expected === null || typeof actual !== 'object' || typeof expected !== 'object') return false;
    if (Array.isArray(actual) !== Array.isArray(expected)) return false;
    const keys = Object.keys(expected);
    return Object.keys(actual).length === keys.length && keys.every(key =>
        Object.prototype.hasOwnProperty.call(actual, key) && equal(actual[key], expected[key]));
}

export function loadInputs(corpus, contract) {
    if (corpus.profile !== 'medium' || corpus.cases.length !== 4 || contract.contract !== 'word-stats-runtime-v1')
        throw new Error('B5 requires the medium corpus and runtime contract v1');
    return corpus.cases.map(item => {
        const fixture = contract.cases.find(c => c.name === `benchmark ${item.name}`);
        if (!fixture || !equal(fixture.input, item.input)) throw new Error(`Missing/stale oracle: ${item.name}`);
        return fixture;
    });
}
