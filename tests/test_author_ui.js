const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

class Node {
  constructor(tag) { this.tag = tag; this.children = []; this.className = ''; this.textContent = ''; }
  append(...children) { this.children.push(...children); }
  setAttribute(name, value) { this[name] = value; }
}
const document = {
  createElement: (tag) => new Node(tag),
  createTextNode: (text) => Object.assign(new Node('#text'), { textContent: text }),
};
const source = fs.readFileSync(path.join(__dirname, '../docs/assets/app.js'), 'utf8');
const context = vm.createContext({ Intl, Date, Number, document });
vm.runInContext(source.slice(0, source.indexOf('const page = document.body.dataset.page;')), context);
const descendants = (node) => [node, ...node.children.flatMap(descendants)];
const authorNodes = (node) => descendants(node).filter((child) => ['authors', 'today-authors'].includes(child.className));
const base = { title: 'Article title', journal: 'System', published: '2026-10-06', score: 0, authors: [] };
const renderers = [context.articleCard, context.todayArticleCard, context.todayOtherArticleRow];
const originals = [
  'Federico Pablo-Martí, Carlos Mir Fernández & Juan de-Lucio',
  'Karla Brücknerová, Jakub Vlček, Jana Navrátilová, Barbora Nekardová, Tomáš Lintner & others',
  'Tuba Stouthart, Dury Bayram, Jan van der Veen',
  '王小明；李华 & bell hooks, ' + Array.from({ length: 60 }, (_, i) => `author ${i}`).join(', '),
  '<script>alert(1)</script> & others',
];
for (const render of renderers) {
  for (const original of originals) {
    const nodes = authorNodes(render({ ...base, authors: ['A differently formatted API author'], authors_text: original }, 0));
    assert.equal(nodes.length, 1);
    assert.equal(nodes[0].textContent, render === context.articleCard ? original : `作者：${original}`);
    // Authors use textContent even if the byline contains markup-like text.
    assert.equal(nodes[0].children.length, 0);
  }
  for (const article of [base, { ...base, authors_text: '' }, { ...base, authors_text: '', authors: ['API Author'] }]) {
    const tree = render(article, 0);
    assert.equal(authorNodes(tree).length, 0);
    assert.ok(descendants(tree).every((node) => !node.textContent.includes('作者信息待补全')));
  }
  assert.equal(authorNodes(render({ ...base, authors: ['Jane Doe', 'John Smith'] }, 0)).length, 1);
}
console.log('Author UI checks passed');
