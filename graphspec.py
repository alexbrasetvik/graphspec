#!/usr/bin/env python
# encoding: utf8

""" graphspec turns textual definitions of graphs into diagrams. """
from __future__ import print_function
from __future__ import unicode_literals

import argparse
import collections
import json
import logging
import os
import random
import re
import sys
import unittest

import flask
import networkx
import sh
from networkx.algorithms import dag, tree
from pyparsing import *

logger = logging.getLogger(__name__)


NODE_STYLES = {
    "highlight": 'style="filled"; fillcolor="pink"',
    "pink": 'style="filled"; fillcolor="pink"',
    "green": 'style="filled"; fillcolor="green"'
}

def err(l): sys.stderr.write(l + '\n')

# Define a parser to find either directives or edge definitions.
# These are samples:
# ..attr: someNode, With a pretty label: color=red; shape=round :: This is a comment
# ..attr: unlabelledNode: color=red; shape=round :: This is a comment
# ..attr: noDataOrLabel ::: Comment on a node without a label or data
#
# Edges can be entities too:
# ..attr: a --> b, Label: fill=red :: Comment

header = Suppress('..') + oneOf('subgraph attr allPaths ancestors descendants')('directive') + Suppress(':')
Identifier = lambda name: Word(alphanums + '._-')(name)
edge = Identifier('start') + Suppress('-->') + Identifier('end')

entity = edge | Identifier('node')
labelledEntity = (entity + Suppress(',') + Regex('[^:]+')('label'))
entityDetails = labelledEntity | entity

comment = Regex('.*')('comment')
data = MatchFirst((
    # Either everything up to a :: and everything that follows after it as a comment
    SkipTo(Literal('::'))('data') + Suppress('::') + comment,
    # ... or simply the rest of the line if there is no comment
    Regex('.*')('data')
))

directive = header + entityDetails + Suppress(':') + data
edgeSpec = MatchFirst((
    edge + Suppress('::') + comment,
    edge
))

parser = directive | edgeSpec


def search_for_statements(line):
    """ Returns all statements found in a line. """
    return [
        dict(r) for r in parser.searchString(line)
    ]


class ParserTests(unittest.TestCase):

    def assertParsedEquals(self, input, expectedData):
        parsed = parser.parseString(input)
        self.assertEqual(dict(parsed), expectedData) #, "Unexpected result for [{}]".format(input))

    def test_edge_parser(self):
        expectedForInput = {
            "edge1 --> edge2 :: comment goes here": {"start": "edge1", "end": "edge2", "comment": "comment goes here"},
            "edge1 --> edge2:: comment goes here": {"start": "edge1", "end": "edge2", "comment": "comment goes here"},

            "edge1 --> edge2": {"start": "edge1", "end": "edge2"},
            "edge1  --> edge2": {"start": "edge1", "end": "edge2"}
        }
        for input, expectedResult in expectedForInput.items():
            self.assertParsedEquals(input, expectedResult)

    def test_parse_directives(self):
        expectedForInput = {
            # Simple attribute directive
            "..attr: nodeName: attrs": {"data": "attrs", "directive": "attr", "node": "nodeName"},
            # Whitespace shouldn't matter
            ".. attr : nodeName : attrs": {"data": "attrs", "directive": "attr", "node": "nodeName"},

            # Add a label
            "..attr: nodeName, Pretty Label: attrs": {"data": "attrs", "directive": "attr", "node": "nodeName", "label": "Pretty Label"},
            # This leaves some trailing whitespace, but that's easy to trim off later
            ".. attr: nodeName , Pretty Label : attrs": {"data": "attrs", "directive": "attr", "node": "nodeName", "label": "Pretty Label "},

            # Comments on directives
            "..attr: nodeName: attrs :: comment goes here": {"data": u"attrs ", "directive": "attr", "node": "nodeName", "comment": "comment goes here"},
            "..attr : nodeName : attrs :: comment goes here": {"data": u"attrs ", "directive": "attr", "node": "nodeName", "comment": "comment goes here"},

            # Label and comments
            "..attr: nodeName, Pretty Label: attrs :: comment goes here": {"data": "attrs ", "directive": "attr", "node": "nodeName", "comment": "comment goes here", "label": "Pretty Label"},
        }

        for input, expectedResult in expectedForInput.items():
            self.assertParsedEquals(input, expectedResult)

        # Test the same but with an edge instead of a node
        sameForEdges = {}
        for key, value in expectedForInput.items():
            value.pop("node", None)
            value["start"] = "edge1"
            value["end"] = "edge2"
            sameForEdges[key.replace("nodeName", "edge1 --> edge2")] = value

        for input, expectedResult in sameForEdges.items():
            self.assertParsedEquals(input, expectedResult)

    def test_real_examples(self):
        expectedForInput = {
            "..attr: a --> b, Label: fill=red :: Comment": {"data": "fill=red ", "start": "a", "end": "b", "label": "Label", "comment": "Comment", "directive": "attr"},
            "a --> b": {"start": "a", "end": "b"},
            "..attr: container --> host.root: fill=red; style=dashed; :: In case of a kernel bug ": {"directive": "attr", "start": "container", "end": "host.root", "data": "fill=red; style=dashed; ", "comment": "In case of a kernel bug "},
            "docker.socket --> host.root :: Docker socket equals root": {"start": "docker.socket", "end": "host.root", "comment": "Docker socket equals root"},
            "..subgraph: grouping: child1, child2": {"directive": "subgraph", "node": "grouping", "data": "child1, child2"}
        }

        for input, expectedResult in expectedForInput.items():
            self.assertParsedEquals(input, expectedResult)

    def test_mixing_statements_and_random_text(self):
        # We should be able to find statements prefixed by whatever, such as prose or code
        expectedForInput = {
            "a --> b": [{"start": "a", "end": "b"}],
            "this could be some code # a --> b": [{"start": "a", "end": "b"}],
            "so could this // a --> b": [{"start": "a", "end": "b"}],
            "two statements // a --> b, b --> c": [{"start": "a", "end": "b"}, {"start": "b", "end": "c"}],
            "Mixing it up: a --> b, ..attr: b --> c, Label: fill=red :: comment here": [
                {"start": "a", "end": "b"},
                {"start": "b", "end": "c", "label": "Label", "directive": "attr", "data": "fill=red ", "comment": "comment here"}
            ],
            "a=42 // foo --> bar :: Comments go to end of line, so no treatment for bar --> baz": [
                {"start": "foo", "end": "bar", "comment": "Comments go to end of line, so no treatment for bar --> baz"}
            ]
        }

        for input, expectedResult in expectedForInput.items():
            self.assertEqual(search_for_statements(input), expectedResult)


class Graph(object):

    def __init__(self, include_everything=False):
        self.g = networkx.DiGraph()
        self.subgraphs = networkx.DiGraph()
        self.include_everything = include_everything
        self.graph_attrs = list()
        self.node_attrs = collections.defaultdict(dict)
        self.edge_attrs = collections.defaultdict(lambda: collections.defaultdict(list))

        self.path_styles = list()
        self.ascendant_styles = list()
        self.descendant_styles = list()

    def include_statement(self, statement):
        if 'start' in statement:
            self._handle_edge_statement(statement)
        else:
            self._handle_node_statement(statement)

    def _handle_edge_statement(self, statement):
        # This could be both an edge- and an attr statement with an edge
        if statement.get('directive', 'attr') != 'attr':
            raise ValueError("Can only handle edges for attr statements, not [{}]".format(statement['directive']))

        start = statement['start']
        end = statement['end']
        data = statement.get('data')

        if data:
            self.edge_attrs[start][end].extend(data.strip().split(";"))

        kwargs = {
            key: statement[key] for key in ('comment', 'label')
            if statement.get(key)
        }

        self.g.add_edge(start, end, **kwargs)

    def _handle_node_statement(self, statement):
        directive = statement['directive']
        node_id = statement['node']

        if directive == 'subgraph':
            self._handle_subgraph(statement)

        elif directive == 'attr':
            if node_id == 'graph':
                self.graph_attrs.append(statement['data'])
            else:
                self.node_attrs[node_id] = statement['data']
                if statement.get('comment'):
                    self.node_attrs[node_id] = 'tooltip={}'.format(json.dumps(statement['comment'])) + statement['data']

    def _handle_subgraph(self, statement):
        children = re.split(' *, *', statement['data'])
        subgraph_id = statement['node']

        for child in children:
            if child:
                self.g.add_edge(subgraph_id, child, is_subgraph_relation=True)

        kwargs = dict(subgraph=True)
        label = statement.get('label')
        if label:
            kwargs['label'] = label

        self.g.node[subgraph_id].update(**kwargs)

    @classmethod
    def from_lines(cls, lines, **kw):
        graph = Graph(**kw)
        for line in lines:
            for statement in search_for_statements(line):
                graph.include_statement(statement)
        return graph

    @classmethod
    def from_string(cls, string, **kw):
        return cls.from_lines(string.split('\n'), **kw)

    def render_node(self, node_id):
        return '"{0}" [id="{0}"; {1}];'.format(node_id, self.node_attrs.get(node_id, ""))

    def render_dot(self):
        dot = ['digraph G {']

        # Graph attributes need to be defined right on the top-level graph
        if self.graph_attrs:
            dot.append("; ".join(self.graph_attrs) + ";")

        # Process any subgraph relations. These define subgraphs (within subgraphs within ...)
        subgraphs = networkx.DiGraph()
        for a, b, edge_data in self.g.edges(data=True):
            if edge_data.get('is_subgraph_relation'):
                subgraphs.add_edge(a, b)

        # Each subgraph must be a tree, a subgraph can't be withing two other subgraphs
        if subgraphs and not tree.is_forest(subgraphs):
            raise ValueError("subgraph mappings must result in trees")

        for ancestor_spec in self.ascendant_styles:
            node_id, attrs = re.match('([^ ]+) *: *(.*)', ancestor_spec).groups()
            attrs = NODE_STYLES.get(attrs, attrs)
            for ancestor in networkx.ancestors(self.g, node_id):
                self.node_attrs[ancestor] = attrs + ';' + self.node_attrs.get(ancestor, '')

        for descendant_spec in self.descendant_styles:
            node_id, attrs = re.match('([^ ]+) *: *(.*)', descendant_spec).groups()
            attrs = NODE_STYLES.get(attrs, attrs)
            for descendant in networkx.descendants(self.g, node_id):
                self.node_attrs[descendant] = attrs + ';' + self.node_attrs.get(descendant, '')

        for path_style_spec in self.path_styles:
            start, end, attrs = re.match('([^ ]+) to ([^ ]+) *: *(.*)', path_style_spec).groups()
            attrs = NODE_STYLES.get(attrs, attrs)

            for nodes_in_path in networkx.all_shortest_paths(self.g, start, end):
                for node in nodes_in_path:
                    self.node_attrs[node] = attrs + ';' + self.node_attrs.get(node, '')

        already_visited = set()
        for node in dag.topological_sort(subgraphs):
            if node in already_visited: continue
            already_visited.add(node)
            if self.g.node[node].get('subgraph'):
                dot.append(self.render_subgraph(node, already_visited))

        # Edges can be defined wherever.
        for a, b in self.g.edges():
            edge_data = self.g.succ[a][b]
            if edge_data.get('is_subgraph_relation'):
                continue

            attrs = ['id="{0}/{1}"'.format(a, b)] + self.edge_attrs[a][b]
            if edge_data.get("comment"):
                attrs.append("tooltip={}".format(json.dumps(edge_data["comment"])))
            if edge_data.get("label"):
                attrs.append("label={}".format(json.dumps(edge_data["label"])))

            dot.append('"{0}" -> "{1}" [{2}]'.format(a, b, "; ".join(attrs)))

        for node_id, data in self.g.nodes(data=True):
            if node_id in already_visited:
                # Node has been part of a subgraph
                continue
            if self.should_include_node(node_id):
                dot.append(self.render_node(node_id))

        dot.append('}')
        return '\n'.join(dot)

    def should_include_node(self, node_id):
        # Only render a node if it has at least one edge going in or out that's _not_ a subgraph relation
        return self.include_everything or self.g.out_degree(node_id) or any(
            not data.get('is_subgraph_relation', False) for _, _, data in self.g.in_edges(node_id, data=True)
        )

    def render_subgraph(self, root, already_visited):
        # Subgraph ids must be prefixed with "cluster_" to be clustered in the renderers,
        # and they cannot contain dots
        dot = ['subgraph cluster_{} '.format(root.replace(".", "_")) + '{', 'id="{}";'.format(root)]

        subgraph_attrs = self.node_attrs.get(root, "")
        if subgraph_attrs:
            dot.append(subgraph_attrs)

        label = self.g.node[root].get("label", '')
        dot.append('label="{}"; style=dashed;'.format(label))

        for child in self.g[root]:
            if child in already_visited: continue
            if self.g.node[child].get('subgraph'):
                subsub = self.render_subgraph(child, already_visited)
                dot.append(subsub)
            else:
                if self.should_include_node(child):
                    dot.append(self.render_node(child))
            already_visited.add(child)

        dot.append('}')

        return '\n'.join(dot)

    def get_graph_data(self):
        ancestors_by_node = {}
        descendants_by_node = {}

        for node in self.g:
            ancestors_by_node[node] = list(networkx.ancestors(self.g, node))
            descendants_by_node[node] = list(networkx.descendants(self.g, node))

        return {
            "transitive_closure": dict(networkx.transitive_closure(self.g).succ._atlas),
            "edges": dict(self.g.succ._atlas)
        }


class GraphTests(unittest.TestCase):

    def graphFromString(self, string):
        graph = Graph()
        for line in string.split('\n'):
            for statement in search_for_statements(line):
                graph.include_statement(statement)
        return graph

    def test_simple_statements(self):
        graph = Graph.from_string("""
a --> b, b --> c
c --> a :: A loop!
..attr: c --> d, Label Goes Here: shape=diamond :: Comments!
        """)

        self.assertEquals(
            graph.get_graph_data()['edges'], {
                "a": {"b": {}},
                "b": {"c": {}},
                "c": {
                    "a": {"comment": "A loop!"},
                    "d": {"comment": "Comments!", "label": "Label Goes Here"}
                },
                "d": {}
            }
        )

    def test_simple_subgraphs(self):
        expectedEdgeData = {
            "a": {"b": {}}, "b": {"c": {}}, "c": {"d": {}}, "d": {},
            "first": {"a": {"is_subgraph_relation": True}, "second": {"is_subgraph_relation": True}},
            "second": {"b": {"is_subgraph_relation": True}, "d": {"is_subgraph_relation": True}}
        }

        lines = """
a --> b
b --> c
c --> d
..subgraph: first, First: a, second
..subgraph: second, Second: b, d
""".split('\n')

        # Jumble up the order of the lines a little, they should not matter.
        for i in range(20):
            random.shuffle(lines)
            graph = Graph.from_lines(lines)

            self.assertEquals(
                graph.get_graph_data()['edges'], expectedEdgeData
            )

    def stripIndentation(self, input):
        """ Returns input with whitespace stripped and empty lines removed"""
        return '\n'.join(l.strip() for l in input.split('\n') if l)

    def test_simple_render(self):
        graph = self.graphFromString("""
a --> b
..attr: b --> c, Label: color=red :: Comment
c --> d
..subgraph: first, First: a, second
..subgraph: second, Second: b, d
""")

        expectedDot = self.stripIndentation("""
digraph G {
    subgraph cluster_first {
        id="first";
        label="First"; style=dashed;
        "a" [id="a"; ];

        subgraph cluster_second {
            id="second";
            label="Second"; style=dashed;
            "b" [id="b"; ];
            "d" [id="d"; ];
        }
    }

"a" -> "b" [id="a/b"]
"c" -> "d" [id="c/d"]
"b" -> "c" [id="b/c"; color=red; tooltip="Comment"; label="Label"]

"c" [id="c"; ];
}""")

        self.assertEquals(graph.render_dot(), expectedDot)


def make_graph_from_dot(dot, layout_engine="dot", format='svg', apply_transitive_reduction=False):
    assert layout_engine in ("dot", "neato", "fdp"), "Unknown layout engine"
    dot_args = ['-Gfontname="Open Sans Light"', '-Efontname="Open Sans Light"', '-Nfontname="Open Sans Light"'] + '-Nshape=plaintext -Gpenwidth=1 -Epenwidth=1 -Gcolor=#bbbbbb -Gratio=compress -T{}'.format(format).split()

    logger.debug("Running [{} {}]".format(layout_engine, ' '.join(dot_args)))

    if apply_transitive_reduction:
        dot = sh.tred(_in=dot).stdout

    return getattr(sh, layout_engine)(*dot_args, _in=dot).stdout


# We'll prepare a very simple web interface, where a profiles file
# can define combinations of annotation sources
app = flask.Flask(__name__, static_url_path='/static/', static_folder="./")
profiles = {}

def get_lines_from_profile(profile):
    if profile.get('shell'):
        # It's some shell command that gets us data
        return os.popen(profile['shell']).read().split('\n')

    elif profile.get('paths'):
        # A list of paths to provide to ripgrep
        cmd = " ".join([
            "rg --no-filename",
            "--only-matching", # This just gets us the excerpts the following patterns actually cover, not anything prior
            "--regexp '([^ ]+) --> ([^ ,]+) *:: *.*'", # An edge with a comment
            "--regexp '\.\.(subgraph|attr|allPaths|ancestors|descendants):.*'", # A statement
            "--regexp '([^ ]+) --> ([^ ,]+)' " # A simple edge
        ] + profile['paths']) # And lastly the paths
        return os.popen(cmd).read().split('\n')


def make_html(svg, graph_data):
    # We inline this so the output is a standalone file
    js = open('graphspec.js').read().decode("utf8")
    css = open('graphspec.css').read().decode("utf8")

    return app.jinja_env.get_template("graph.html").render(svg=svg, js=js, css=css, graph_data=graph_data).encode("utf8")


@app.route('/', methods=['GET'])
def root():
    return flask.render_template("profile-list.html", profiles=profiles)


@app.route('/static/<path:path>')
def static_file(path):
    return flask.send_static_file(path)


@app.route('/<profile_name>', methods=['GET'])
def profile(profile_name):
    profile = profiles.get(profile_name)
    if not profile:
        return 'no such profile', 404
    lines = get_lines_from_profile(profile)

    include_everything = flask.request.args.get("include_everything", profile.get("include_everything", False))
    graph = Graph.from_lines(lines, include_everything=include_everything)
    graph_data = graph.get_graph_data()
    apply_transitive_reduction = profile.get("apply_transitive_reduction", flask.request.args.get("apply_transitive_reduction", False))
    layout_engine = flask.request.args.get("layout_engine", profile.get("layout_engine", "dot"))

    svg = make_graph_from_dot(graph.render_dot().encode("utf8"), layout_engine, "svg", apply_transitive_reduction).decode("utf8")

    return make_html(svg, graph_data)


def main():
    parser = argparse.ArgumentParser(description='')
    parser.add_argument("--test", help="Runs tests", action="store_true")
    parser.add_argument("--dot", help="Emit the Graphviz data without further processing", action="store_true")
    parser.add_argument("--pdf", help="Output a PDF", action="store_true")
    parser.add_argument("--png", help="Output a PNG", action="store_true")
    parser.add_argument("--svg", help="Output an SVG", action="store_true")
    parser.add_argument("--html", help="Output an HTML", action="store_true")
    parser.add_argument("--json", help="Output a JSON dump of the graph data", action="store_true")
    parser.add_argument("--serve", help="Start an HTTP server", action="store_true")
    parser.add_argument("--port", help="Port to bind to, if serving", action="store", default="8008")
    parser.add_argument("--host", help="Host to bind to, if serving", action="store", default="127.0.0.1")
    parser.add_argument("--profile", help="Profiles to serve, if serving", action="store")
    parser.add_argument("--type", help="Graph type: dot, neato, or fdp", action="store", default="dot")
    parser.add_argument("--include-everything", help="Include nodes with no in- or outputs?", action="store_true")

    args = parser.parse_args()
    
    if not any((args.dot, args.pdf, args.png, args.svg, args.html, args.json, args.serve, args.test)):
        parser.print_help()
        return

    if args.test:
        import doctest
        doctest.testmod(raise_on_error=True)
        sys.argv.remove(("--test"))
        unittest.main(verbosity=2)
        return

    elif args.serve:
        import yaml
        print("Starting to serve up profiles from [{}]".format(args.profile))
        print("Warning: Don't assume this service is safe to run on a public interface.")
        print("Warning: Note that anyone that can edit the profile file can run arbitrary code.")
        profiles.update(yaml.safe_load(open(args.profile)))
        app.run(host=args.host, port=int(args.port))
        return

    # We'll be wanting a graph
    graph = Graph.from_string(sys.stdin.read().decode("utf8"), include_everything=args.include_everything)
    dot = graph.render_dot().encode("utf8")
    graph_data = graph.get_graph_data()

    if args.dot:
        # Just the dot please
        print(dot)
        return
    elif args.json:
        # The JSON can be useful to e.g. run tests on
        print(json.dumps(graph_data, indent=4))
        return

    # At this point we'll be invoking graphviz to generate a graph
    format = (args.pdf and 'pdf') or (args.png and 'png') or 'svg'
    rendered_graph = make_graph_from_dot(dot, args.type, format)

    if args.pdf or args.svg or args.png:
        # We just want the graph
        print(rendered_graph)
        return

    assert args.html
    # At this point we want a self-contained HTML file
    svg = rendered_graph.decode("utf8")
    print(make_html(svg, graph_data))

if __name__ == '__main__':
    main()