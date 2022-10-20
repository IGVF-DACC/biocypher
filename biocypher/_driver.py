#!/usr/bin/env python

#
# Copyright 2021, Heidelberg University Clinic
#
# File author(s): Sebastian Lobentanzer
#                 ...
#
# Distributed under GPLv3 license, see the file `LICENSE`.
#

"""
Biocypher specific database management and access methods.
"""
from collections.abc import Iterable

from ._logger import logger

logger.debug(f'Loading module {__name__}.')

from typing import TYPE_CHECKING, Any, Optional
import inspect
import itertools

from more_itertools import peekable

if TYPE_CHECKING:

    import neo4j

import neo4j_utils

from . import _misc
from ._write import BatchWriter
from ._config import config as _config
from ._create import VersionNode, BioCypherEdge, BioCypherNode
from ._translate import Translator, BiolinkAdapter

__all__ = ['Driver']


class Driver(neo4j_utils.Driver):
    """
    Manages a connection to a biocypher database.
    """

    def __init__(
        self,
        driver: Optional['neo4j.Driver'] = None,
        db_name: Optional[str] = None,
        db_uri: Optional[str] = None,
        db_user: Optional[str] = None,
        db_passwd: Optional[str] = None,
        fetch_size: int = 1000,
        raise_errors: Optional[bool] = None,
        wipe: bool = False,
        offline: bool = False,
        increment_version=True,
        user_schema_config_path: Optional[str] = None,
        delimiter: Optional[str] = None,
        array_delimiter: Optional[str] = None,
        quote_char: Optional[str] = None,
        skip_bad_relationships: bool = False,
        skip_duplicate_nodes: bool = False,
    ):
        """
        Set up a BioCypher database connection.

        The connection can be defined in three ways:
        * Providing a ready ``neo4j.Driver`` instance
        * By URI and authentication data
        * By a YAML config file

        Args:
            driver:
                A ``neo4j.Driver`` instance, created by, for example,
                ``neo4j.GraphDatabase.driver``.
            db_name:
                Name of the database (Neo4j graph) to use.
            db_uri:
                Protocol, host and port to access the Neo4j server.
            db_user:
                Neo4j user name.
            db_passwd:
                Password of the Neo4j user.
            fetch_size:
                Optional; the fetch size to use in database transactions.
            wipe:
                Wipe the database after connection, ensuring the data is
                loaded into an empty database.
            increment_version:
                Whether to increase version number automatically and create a
                new BioCypher version node in the graph.
            schema_config:
                Path to a custom database schema configuration file.
            delimiter:
                Delimiter for CSV export.
            quote_char:
                String quotation character for CSV export.
            array_delimiter:
                Array delimiter for CSV exported contents.
        """

        driver_args = {
            arg: _misc.if_none(locals().get(arg, None), _config(arg))
            for arg in inspect.signature(neo4j_utils.Driver).keys()
        }

        self.csv_delim = delimiter or _config('csv_delimiter')
        self.csv_adelim = array_delimiter or _config('csv_array_delimiter')
        self.csv_quote = quote_char or _config('csv_quote_char')

        self.skip_bad_relationships = skip_bad_relationships
        self.skip_duplicate_nodes = skip_duplicate_nodes

        neo4j_utils.Driver.__init__(**driver_args)
        # get database version node ('check' module) immutable
        # variable of each instance (ie, each call from the
        # adapter to BioCypher); checks for existence of graph
        # representation and returns if found, else creates new
        # one
        self.db_meta = VersionNode(
            from_config=offline or wipe,
            config_file=user_schema_config_path,
            offline=offline,
            bcy_driver=self,
        )

        # if db representation node does not exist or explicitly
        # asked for wipe, create new graph representation: default
        # yaml, interactive?
        # Denes: those are two different cases, if it's wiped, first
        # its contents should be read, if it does not exist, a new one
        # should be created.

        # likely this will be refactored soon
        self._create_constraints()

        if increment_version:

            # set new current version node
            self.update_meta_graph()

        self.bl_adapter = None
        self.batch_writer = None
        self._update_translator()

        # TODO: implement passing a driver instance
        # Denes: I am not sure, but seems like it works already
        # by the base class

    def update_meta_graph(self):
        """
        Write the current schema and config state into the database.

        The meta graph harbors the config parameters, the schema and the
        history of the database.
        """

        if self.offline:
            return

        logger.info('Updating Neo4j meta graph.')
        # add version node
        self.add_biocypher_nodes(self.db_meta)

        # find current version node
        db_version = self.query(
            'MATCH (v:BioCypher) ' 'WHERE NOT (v)-[:PRECEDES]->() ' 'RETURN v',
        )
        # connect version node to previous
        if db_version[0]:

            e_meta = BioCypherEdge(
                self.db_meta.graph_state['id'],
                self.db_meta.node_id,
                'PRECEDES',
            )
            self.add_biocypher_edges(e_meta)

        # add structure nodes
        no_l = []
        # leaves of the hierarchy specified in schema yaml
        for entity, params in self.db_meta.leaves.items():

            no_l.append(
                BioCypherNode(
                    node_id=entity, node_label='MetaNode', properties=params,
                ),
            )

        self.add_biocypher_nodes(no_l)

        # remove connection of structure nodes from previous version
        # node(s)
        self.query('MATCH ()-[r:CONTAINS]-()' 'DELETE r')

        # connect structure nodes to version node
        ed_v = []
        current_version = self.db_meta.get_id()
        for entity in self.db_meta.leaves.keys():

            ed_v.append(
                BioCypherEdge(
                    source_id=current_version,
                    target_id=entity,
                    relationship_label='CONTAINS',
                ),
            )

        self.add_biocypher_edges(ed_v)

        # add graph structure between MetaNodes
        ed = []
        for no in no_l:

            _id = no.get_id()
            src = no.get_properties().get('source')
            tar = no.get_properties().get('target')

            if not any(x is None for x in (_id, src, tar)):

                ed.append(BioCypherEdge(_id, src, 'IS_SOURCE_OF'))
                ed.append(BioCypherEdge(_id, tar, 'IS_TARGET_OF'))

        self.add_biocypher_edges(ed)

    def _update_translator(self):

        self.translator = Translator(leaves=self.db_meta.leaves)

    def init_db(self):
        """
        Wipes the database and creates constraints.

        Used to initialise a property graph database by deleting
        contents and constraints and setting up new constraints.

        Todo:
            - Set up constraint creation interactively depending on the
              need of the database
        """

        self.wipe_db()
        self._create_constraints()
        logger.info('Initialising database.')

    def _create_constraints(self):
        """
        Creates constraints on node types in the graph.

        Used for initial setup. Grabs leaves of the ``schema_config.yaml``
        file and creates constraints on the id of all entities represented as
        nodes.
        """

        logger.info('Creating constraints for node types in config.')

        # get structure
        for leaf in self.db_meta.leaves.items():
            label = leaf[0]
            if leaf[1]['represented_as'] == 'node':

                s = (
                    f'CREATE CONSTRAINT {label}_id '
                    f'IF NOT EXISTS ON (n:{label}) '
                    'ASSERT n.id IS UNIQUE'
                )
                self.query(s)

    def add_nodes(
            self,
            nodes: Iterable[
                tuple[
                    str,
                    str,
                    dict[str, Any],
                ]
            ],
    ) -> tuple:
        """
        Translate nodes and write them into the database.

        Generic node adder method to add any kind of input to the
        graph via the :class:`biocypher.create.BioCypherNode` class. Employs
        translation functionality and calls the :meth:`add_biocypher_nodes()`
        method.

        Args:
            nodes:
                For each node to add to the biocypher graph, a 3-tuple with
                the following layout:
                * The (unique if constrained) ID of the node.
                * The type of the node, capitalised or PascalCase and in noun
                  form (Neo4j primary label, eg `:Protein`).
                * A dictionary of arbitrary properties the node should
                  possess (can be empty).

        Returns:
            2-tuple: the query result of :meth:`add_biocypher_nodes()`
            - first entry: data
            - second entry: Neo4j summary.
        """

        bn = self.translator.translate_nodes(nodes)
        return self.add_biocypher_nodes(bn)

    def add_edges(
            self,
            edges: Iterable[
                tuple[
                    Optional[str],
                    str,
                    str,
                    str,
                    dict[str, Any],
                ]
            ],
    ) -> tuple:
        """
        Translate edges and write them into the database.

        Generic edge adder method to add any kind of input to the graph
        via the :class:`biocypher.create.BioCypherEdge` class. Employs
        translation functionality and calls the
        :meth:`add_biocypher_edges()` method.

        Args:
            id_src_tar_type_tuples (iterable of 5-tuple):
                For each edge to add to the biocypher graph, a 5-tuple
                with the following layout:
                * The optional unique ID of the interaction. This can be
                  `None` if there is no systematic identifier (which for
                  many interactions is the case).
                * The (unique if constrained) ID of the source node of the
                  relationship.
                * Same for the target node.
                * The type of the relationship.
                * A dictionary of arbitrary properties the edge should
                  possess (can be empty).

        Returns:
            2-tuple: the query result of :meth:`add_biocypher_edges()`
            - first entry: data
            - second entry: Neo4j summary.
        """

        be = self.translator.translate_edges(edges)
        return self.add_biocypher_edges(be)

    def add_biocypher_nodes(
            self,
            nodes: Iterable[BioCypherNode],
            explain: bool = False,
            profile: bool = False,
    ) -> bool:
        """
        Write nodes into the database.

        Accepts a node type handoff class
        (:class:`biocypher.create.BioCypherNode`) with id,
        label, and a dict of properties (passing on the type of
        property, ie, ``int``, ``str``, ...).

        The dict retrieved by the
        :meth:`biocypher.create.BioCypherNode.get_dict()` method is
        passed into Neo4j as a map of maps, explicitly encoding node id
        and label, and adding all other properties from the 'properties'
        key of the dict. The merge is performed via APOC, matching only
        on node id to prevent duplicates. The same properties are set on
        match and on create, irrespective of the actual event.

        Args:
            nodes:
                An iterable of :class:`biocypher.create.BioCypherNode` objects.
            explain:
                Call ``EXPLAIN`` on the CYPHER query.
            profile:
                Do profiling on the CYPHER query.

        Returns:
            `True` for success, `False` otherwise.
        """

        try:

            entities = [
                node.get_dict() for node in _misc.ensure_iterable(nodes)
            ]

        except AttributeError as e:

            msg = f'Nodes must have a `get_dict` method: {str(e)}'
            logger.error(msg)

            raise TypeError(msg)

        logger.info(f'Merging {len(entities)} nodes.')

        entity_query = (
            'UNWIND $entities AS ent '
            'CALL apoc.merge.node([ent.node_label], '
            '{id: ent.node_id}, ent.properties, ent.properties) '
            'YIELD node '
            'RETURN node'
        )

        method = 'explain' if explain else 'profile' if profile else 'query'

        result = getattr(self, method)(
            entity_query,
            parameters={'entities': entities},
        )

        logger.info('Finished merging nodes.')

        return result

    def add_biocypher_edges(
            self,
            edges: Iterable[BioCypherEdge],
            explain: bool = False,
            profile: bool = False,
    ) -> bool:
        """
        Write edges into the database.

        Accepts an edge type handoff class
        (:class:`biocypher.create.BioCypherEdge`) with source
        and target ids, label, and a dict of properties (passing on the
        type of property, ie, int, string ...).

        The individual edge is either passed as a singleton, in the case
        of representation as an edge in the graph, or as a 4-tuple, in
        the case of representation as a node (with two edges connecting
        to interaction partners).

        The dict retrieved by the
        :meth:`biocypher.create.BioCypherEdge.get_dict()` method is
        passed into Neo4j as a map of maps, explicitly encoding source
        and target ids and the relationship label, and adding all edge
        properties from the 'properties' key of the dict. The merge is
        performed via APOC, matching only on source and target id to
        prevent duplicates. The same properties are set on match and on
        create, irrespective of the actual event.

        Args:
            edges:
                An iterable of :class:`biocypher.create.BioCypherEdge` objects.
            explain:
                Call ``EXPLAIN`` on the CYPHER query.
            profile:
                Do profiling on the CYPHER query.

        Returns:
            `True` for success, `False` otherwise.
        """

        edges = _misc.ensure_iterable(edges)
        edges = itertools.chain(*(_misc.ensure_iterable(i) for i in edges))

        nodes = []
        rels = []

        try:

            for e in edges:

                if hasattr(e, 'get_node'):

                    nodes.append(e.get_node())
                    rels.append(e.get_source_edge().get_dict())
                    rels.append(e.get_target_edge().get_dict())

                else:

                    rels.append(e.get_dict())

        except AttributeError as e:

            msg = f'Edges and nodes must have a `get_dict` method: {str(e)}'
            logger.error(msg)

            raise TypeError(msg)

        self.add_biocypher_nodes(nodes)
        logger.info(f'Merging {len(rels)} edges.')

        # cypher query

        # merging only on the ids of the entities, passing the
        # properties on match and on create;
        # TODO add node labels?
        node_query = (
            'UNWIND $rels AS r '
            'MERGE (src {id: r.source_id}) '
            'MERGE (tar {id: r.target_id}) '
        )

        self.query(node_query, parameters={'rels': rels})

        edge_query = (
            'UNWIND $rels AS r '
            'MATCH (src {id: r.source_id}) '
            'MATCH (tar {id: r.target_id}) '
            'WITH src, tar, r '
            'CALL apoc.merge.relationship'
            '(src, r.relationship_label, NULL, '
            'r.properties, tar, r.properties) '
            'YIELD rel '
            'RETURN rel'
        )

        method = 'explain' if explain else 'profile' if profile else 'query'

        result = getattr(self, method)(edge_query, parameters={'rels': rels})

        logger.info('Finished merging edges.')

        return result

    def write_nodes(
            self,
            nodes: Iterable[BioCypherNode],
            dirname: Optional[str] = None,
            db_name: Optional[str] = None,

    ) -> bool:
        """
        Write the nodes into CSV file.

        Write BioCypher nodes to disk using the :mod:`write` module,
        formatting the CSV to enable Neo4j admin import from the target
        directory.

        Args:
            nodes:
                Collection of nodes to be written in
                BioCypher-compatible CSV format; can be any compatible
                (ie, translatable) input format or already as
                :class:`biocypher.create.BioCypherNode`.

        Returns:
            Whether the write was successful.
        """

        # instantiate adapter on demand because it takes time to load
        # the biolink model toolkit
        self.start_bl_adapter()

        self.start_batch_writer(dirname, db_name)

        nodes = peekable(nodes)
        if not isinstance(nodes.peek(), BioCypherNode):
            tnodes = self.translator.translate_nodes(nodes)
        else:
            tnodes = nodes
        # write node files
        return self.batch_writer.write_nodes(tnodes)

    def start_batch_writer(
        self,
        dirname: str,
        db_name: str,
    ) -> None:
        """
        Instantiate the batch writer if it does not exist.

        Args:
            dirname:
                The directory to write the files to.
            db_name:
                The name of the database to write the files to.
        """
        if not self.batch_writer:

            self.batch_writer = BatchWriter(
                leaves=self.db_meta.leaves,
                bl_adapter=self.bl_adapter,
                delimiter=self.csv_delim,
                array_delimiter=self.csv_adelim,
                quote=self.csv_quote,
                dirname=dirname,
                db_name=db_name or self.current_db,
                skip_bad_relationships=self.skip_bad_relationships,
                skip_duplicate_nodes=self.skip_duplicate_nodes,
            )

    def start_bl_adapter(self):
        """
        Makes sure a Biolink adapter is available.

        Instantiate the :class:`biocypher.adapter.BioLinkAdapter` if not
        existing.

        Attributes:
            bl_adapter:
                An instance of :class:`biocypher.adapter.BioLinkAdapter`.
        """
        if not self.bl_adapter:
            self.bl_adapter = BiolinkAdapter(leaves=self.db_meta.leaves)

    def write_edges(
            self,
            edges: Iterable[BioCypherEdge],
            db_name: str = None,
            dirname: str = None,
    ) -> bool:
        """
        Write the edges to CSV file.

        Write BioCypher edges to disk using the :mod:`write` module,
        formatting the CSV to enable Neo4j admin import from the target
        directory.

        Args:
            edges:
                Collection of edges to be written in BioCypher-compatible
                CSV format; can be any compatible (ie, translatable) input
                format or already as :class:`biocypher.create.BioCypherEdge`.

        Returns:
            Whether the write was successful.
        """

        # instantiate adapter on demand because it takes time to load
        # the biolink model toolkit
        self.start_bl_adapter()

        self.start_batch_writer(dirname, db_name)

        edges = peekable(edges)
        if not isinstance(edges.peek(), BioCypherEdge):
            tedges = self.translator.translate_edges(edges)
        else:
            tedges = edges
        # write edge files
        return self.batch_writer.write_edges(tedges)

    def get_import_call(self) -> str:
        """
        Create a *neo4j-admin* CLI call that imports the generated CSV files.

        Upon using the batch writer for writing admin import CSV files,
        return a string containing the neo4j admin import call with
        delimiters, database name, and paths of node and edge files.

        Returns:
            A *neo4j-admin* import call.
        """
        return self.batch_writer.get_import_call()

    def write_import_call(self) -> bool:
        """
        Write the *neo4j-admin* CLI call into a file.

        Upon using the batch writer for writing admin import CSV files,
        write a string containing the neo4j admin import call with
        delimiters, database name, and paths of node and edge files, to
        the export directory.

        Returns:
            The write was successful.
        """
        return self.batch_writer.write_import_call()

    def log_missing_bl_types(self) -> Optional[set[str]]:
        """
        Send log message about Biolink types missing from the schema config.

        Get the set of Biolink types encountered without an entry in
        the `schema_config.yaml` and print them to the logger.

        Returns:
            A set of missing Biolink types
        """

        mt = self.translator.get_missing_bl_types()

        if mt:
            msg = (
                'Input entities not accounted for due to them not being '
                'present in the `schema_config.yaml` configuration file '
                '(see log for details): \n'
            )
            for k, v in mt.items():
                msg += f'    {k}: {v} \n'

            logger.warning(msg)

        else:

            logger.info('No missing Biolink types in input.')

    def log_duplicates(self):
        """
        Get the set of duplicate nodes and edges encountered and print them to
        the logger.
        """

        dtypes = self.batch_writer.get_duplicate_node_types()

        if dtypes:
            logger.warning(
                'Duplicate nodes encountered in the following types '
                '(see log for details): \n'
                f'{dtypes}',
            )

            dn = self.batch_writer.get_duplicate_nodes()

            msg = 'Duplicate nodes encountered: \n'
            for k, v in dn.items():
                msg += f'    {k}: {v} \n'

            logger.debug(msg)

        else:
            logger.info('No duplicate nodes in input.')

        etypes = self.batch_writer.get_duplicate_edge_types()

        if etypes:
            logger.warning(
                'Duplicate edges encountered in the following types '
                '(see log for details): \n'
                f'{etypes}',
            )

            de = self.batch_writer.get_duplicate_edges()

            msg = 'Duplicate edges encountered: \n'
            for k, v in de.items():
                msg += f'    {k}: {v} \n'

            logger.debug(msg)

        else:
            logger.info('No duplicate edges in input.')

    # TRANSLATION METHODS #

    def translate_term(self, term: str) -> str:
        """
        Translate a term to its BioCypher equivalent.
        """

        # instantiate adapter if not exists
        self.start_bl_adapter()

        return self.bl_adapter.translate_term(term)

    def reverse_translate_term(self, term: str) -> str:
        """
        Reverse translate a term from its BioCypher equivalent.
        """

        # instantiate adapter if not exists
        self.start_bl_adapter()

        return self.bl_adapter.reverse_translate_term(term)

    def translate_query(self, query: str) -> str:
        """
        Translate a query to its BioCypher equivalent.
        """

        # instantiate adapter if not exists
        self.start_bl_adapter()

        return self.bl_adapter.translate(query)

    def reverse_translate_query(self, query: str) -> str:
        """
        Reverse translate a query from its BioCypher equivalent.
        """

        # instantiate adapter if not exists
        self.start_bl_adapter()

        return self.bl_adapter.reverse_translate(query)

    def __repr__(self):

        return f'<BioCypher {neo4j_utils.Driver.__repr__(self)[1:]}'
