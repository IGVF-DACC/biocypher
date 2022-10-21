#!/usr/bin/env python

#
# Copyright 2021, Heidelberg University Clinic
#
# File author(s): Sebastian Lobentanzer
#                 ...
#
# Distributed under GPLv3 license, see the file `LICENSE`.
#

from typing import Literal
from datetime import datetime

import yaml

from . import _misc
from . import _config as config
from ._logger import logger

if TYPE_CHECKING:

    from biocypher._driver import Driver

__all__ = ['VersionNode']


class VersionNode:
    """
    Versioning and graph structure information meta node.

    Similar to BioCypherNode but fixes label to ":BioCypher" and sets
    version by using the current date and time (meaning it overrides both
    mandatory args from BioCypherNode).

    Is created upon establishment of connection with the database and remains
    fixed for each BioCypher "session" (ie, the entire duration from starting
    the connection to the termination of the BioCypher adapter instance). Is
    connected to MetaNodes and MetaEdges via ":CONTAINS" relationships.
    """

    def __init__(
            self,
            offline: bool = False,
            from_config: bool = False,
            config_file: str = None,
            node_label: str = 'BioCypher',
            bcy_driver: 'Driver' = None,
    ):
        """
        Create a node with schema and version information.

        Args:
            offline:
                No connection to server.
            from_config:
                Read the parameters from config, instead of an existing
                node in the database.
            config_file:
                Path to config file.
            node_label:
                Label of the version node.
            bcy_driver:
                A driver instance that supports the connection and already
                carries config data.
        """

        # if we do not have a driver, then likely we are offline, right?
        self.offline = offline or getattr(bcy_driver, 'offline', True)
        self.from_config = from_config
        self.config_file = config_file
        self.node_label = node_label
        self.bcy_driver = bcy_driver

        self.node_id = self._timestamp
        self.graph_state = (
            self._get_graph_state() if not self.offline else None
        )
        self.update_schema()
        self.leaves = self._get_leaves()

        self.properties = {
            'graph_state': self.graph_state,
            'schema': self.schema,
            'leaves': self.leaves,
        }

    def get_id(self) -> str:
        """
        Returns primary node identifier.

        Returns:
            str: node_id
        """
        return self.node_id

    def get_label(self) -> str:
        """
        Returns primary node label.

        Returns:
            str: node_label
        """
        return self.node_label

    def get_dict(self) -> dict:
        """
        Return dict of id, labels, and properties.

        Returns:
            dict: node_id and node_label as top-level key-value pairs,
            properties as second-level dict.
        """
        return {
            'node_id': self.node_id,
            'node_label': self.node_label,
            'properties': self.properties,
        }

    @property
    def _timestamp(self):
        """
        A timestampt that serves as unique ID for the current session.

        Instantiate a version ID for the current session. For now does
        versioning using datetime.

        Can later implement incremental versioning, versioning from
        config file, or manual specification via argument.
        """

        now = datetime.now()
        return now.strftime('v%Y%m%d-%H%M%S')

    @property
    def node_id(self):
        """
        Unique ID of the current session.
        """

        return self._node_id

    @node_id.setter
    def node_id(self, node_id: str) -> str:
        """
        Unique ID of the current session.
        """

        if hasattr(self, '_node_id'):

            raise TypeError('Changing `node_id` is not supported.')

        else:

            self._node_id = node_id

    def _get_graph_state(self):
        """
        Current graph state if available, otherwise create a new one.

        Check in active DBMS connection for existence of VersionNodes,
        return the most recent VersionNode as representation of the
        graph state. If no VersionNode found, assume blank graph state
        and initialise.
        """

        logger.info('Getting graph state.')

        result, summary = self.bcy_driver.query(
            'MATCH (meta:BioCypher)'
            'WHERE NOT (meta)-[:PRECEDES]->(:BioCypher)'
            'RETURN meta',
        )

        # if result is empty, initialise
        if not result:
            logger.info('No existing graph found, initialising.')
            return {'id': self.node_id}
        # else, pass on graph state
        else:
            version = result[0]['meta']['id']
            logger.info(f'Found graph state at {version}.')
            return result[0]['meta']

    def read_schema(
        self,
        from_config: bool | None = None,
        config_file: str | None = None,
    ):
        """
        Read the schema either from the graph or from a config file.

        Args:
            from_config:
                Load the schema from the config file even if schema
                in the current database exists.
            config_file:
                Path to a config file. If not provided here or at the
                instance level, the built-in default will be used.

        Attributes:
            schema:
                Graph schema information from meta graph if it exists, or
                create new schema information properties from configuration
                file.
        """

        from_config = _misc.if_none(from_config, self.from_config)

        self.schema = {} if from_config else self.schema_from_db()

        if not self.schema:

            self.schema = self.schema_from_config(config_file = config_file)

    def schema_from_db(self) -> dict:
        """
        Read the schema encoded in the graph meta nodes.
        """

        # TODO do we want information about actual structure here?
        res = self.bcy_driver.query(
            'MATCH (src:MetaNode) '
            # "OPTIONAL MATCH (src)-[r]->(tar)"
            'RETURN src',  # , type(r) AS type, tar"
        )

        return {r['src'].pop('id'): r['src'] for r in res[0]}

    def schema_from_config(self, config_file: str | None = None) -> dict:
        """
        Read the schema from a config file.

        Args:
            config_file:
                Path to a config file. If not provided here or at the
                instance level, the built-in default will be used.
        """

        config_file = config_file or self.config_file

        if config_file:

            with open(config_file) as f:

                schema = yaml.safe_load(f)
        else:

            schema = config.module_data('schema_config')

        return schema

    def update_leaves(self, schema: dict | None = None) -> dict:
        """
        Leaves of the schema.

        Get leaves of the tree hierarchy from the data structure dict
        contained in the `schema_config.yaml`. Creates virtual leaves
        (as children) from entries that provide more than one preferred
        id type (and corresponding inputs).

        Args:
            schema:
                Data structure as loaded by ``update_schema``.

        Attributes:
            leaves:
                Leaves in the database schema.
        """

        self.leaves = self.find_leaves(schema = schema or self.schema)

    @staticmethod
    def find_leaves(schema: dict) -> dict:
        """
        Leaves from schema.

        Args:
            schema:
                Database schema as loaded by ``update_schema``.

        Returns:
            Leaves in the database schema.
        """

        leaves = {}

        # first pass: get parent leaves with direct representation in ontology
        leaves = {
            k: v
            for k, v in schema.items()
            if 'is_a' not in v and 'represented_as' in v
        }

        # second pass: "vertical" inheritance
        schema = self._vertical_property_inheritance(schema)

        # create leaves for all straight descendants (no multiple identifiers
        # or sources) -> explicit children
        # TODO do we need to order children by depth from real leaves?
        leaves.update({k: v for k, v in schema.items() if 'is_a' in v})

        # "horizontal" inheritance: create siblings for multiple identifiers
        # or sources -> virtual leaves or implicit children
        mi_leaves = {}
        ms_leaves = {}

        for k, v in d.items():

            # k is not an entity
            if 'represented_as' not in v:
                continue

            if isinstance(v.get('preferred_id'), list):
                mi_leaves = self._horizontal_inheritance_pid(k, v)
                leaves.update(mi_leaves)

            elif isinstance(v.get('source'), list):
                ms_leaves = self._horizontal_inheritance_source(k, v)
                leaves.update(ms_leaves)

        return leaves

    def _vertical_property_inheritance(self, schema: dict) -> dict:
        """
        Inherit properties from parents to children.
        """

        def copy_key(d0, d1, key):

            if key in d0:

                d1[key] = d0[key]


        for k, v in schema.items():

            # k is not an entity or present in the ontology
            if 'represented_as' not in v or 'is_a' not in v:

                continue

            # "vertical" inheritance: inherit properties from parent
            if v.get('inherit_properties', False):

                # get direct ancestor
                parent = _misc.first(v['is_a'])
                # update properties of child
                copy_key(self.schema[parent], v, 'properties')
                copy_key(self.schema[parent], v, 'exclude_properties')

        return schema

    def _horizontal_inheritance(
            self,
            key: str,
            value: dict,
            by: Literal['source', 'preferred_id'],
        ) -> dict:
        """
        Create virtual leaves for multiple sources or preferred IDs.

        If we create virtual leaves, label_in_input always has to be a list.
        """

        leaves = {}

        variables = (by, 'label_in_input', 'represented_as')

        length = (
            len(value['source'])
                if by == 'source' else
            max(len(_misc.to_list(value[v])) for v in variables)
        )

        values = tuple(
            [value[v]] * length
                if isinstance(value[v], str) else
            value[v]
            for v in variables
        )

        for _by, lab, rep in zip(*values):

            skey = f'{_by}.{key}'
            leaves[skey] = value.copy()
            leaves[skey].update({
                by: _by,
                'label_in_input': lab,
                'represented_as': rep,
                'virtual': True,
                'is_a': [key] + _misc.to_list(value.get('is_a', []))
            })

        return leaves

    def _horizontal_inheritance_pid(self, key, value):
        """
        Create virtual leaves for multiple preferred id types or sources.

        If we create virtual leaves, label_in_input always has to be a list.
        """

        return self._horizontal_inheritance(
            key = key,
            value = value,
            by = 'preferred_id',
        )

    def _horizontal_inheritance_source(self, key, value):
        """
        Create virtual leaves for multiple sources.

        If we create virtual leaves, label_in_input always has to be a list.
        """

        return self._horizontal_inheritance(
            key = key,
            value = value,
            by = 'source',
        )
