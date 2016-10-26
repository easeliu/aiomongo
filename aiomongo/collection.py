import collections
from typing import Iterable, Optional, Union, List, Tuple, MutableMapping

from bson import ObjectId
from bson.son import SON
from bson.codec_options import CodecOptions
from pymongo import common, message
from pymongo.helpers import (_check_write_command_response, _index_document,
                             _index_list, _gen_index_name)
from pymongo.read_preferences import ReadPreference
from pymongo.results import InsertManyResult, InsertOneResult

from .bulk import Bulk
from .command_cursor import CommandCursor
from .cursor import Cursor


class Collection:

    def __init__(self, database, name, read_preference=None, read_concern=None, codec_options=None,
                 write_concern=None):
        self.database = database
        self.read_preference = read_preference or database.read_preference
        self.read_concern = read_concern or database.read_concern
        self.write_concern = write_concern or database.write_concern
        self.codec_options = codec_options or database.codec_options
        self.name = name

        self.__write_response_codec_options = self.codec_options._replace(
            unicode_decode_error_handler='replace',
            document_class=dict)

    def __str__(self):
        return '{}.{}'.format(self.database.name, self.name)

    def __repr__(self) -> str:
        return 'Collection({}, {})'.format(self.database.name, self.name)

    async def __create(self, options: dict):
        """Sends a create command with the given options.
        """
        cmd = SON([('create', self.name)])
        if options:
            if 'size' in options:
                options['size'] = float(options['size'])
            cmd.update(options)

        connection = self.database.client.get_connection()
        await connection.command(
            self.database.name, cmd, ReadPreference.PRIMARY, self.codec_options
        )

    async def count(self, filter: Optional[dict]=None, hint: Optional[Union[str, List[Tuple]]]=None,
                    limit: Optional[int]=None, skip: Optional[int]=None, max_time_ms: Optional[int]=None) -> int:
        cmd = SON([('count', self.name)])
        if filter is not None:
            cmd['query'] = filter
        if hint is not None and not isinstance(hint, str):
            cmd['hint'] = _index_document(hint)
        if limit is not None:
            cmd['limit'] = limit
        if skip is not None:
            cmd['skip'] = skip
        if max_time_ms is not None:
            cmd['maxTimeMS'] = max_time_ms

        connection = self.database.client.get_connection()

        result = await connection.command(
            self.database.name, cmd, self.read_preference, self.__write_response_codec_options,
            read_concern=self.read_concern, allowable_errors=['ns missing']
        )

        if result.get('errmsg', '') == 'ns missing':
            return 0

        return int(result["n"])

    async def create_index(self, keys, **kwargs):
        """Creates an index on this collection.

        Takes either a single key or a list of (key, direction) pairs.
        The key(s) must be an instance of :class:`basestring`
        (:class:`str` in python 3), and the direction(s) must be one of
        (:data:`~pymongo.ASCENDING`, :data:`~pymongo.DESCENDING`,
        :data:`~pymongo.GEO2D`, :data:`~pymongo.GEOHAYSTACK`,
        :data:`~pymongo.GEOSPHERE`, :data:`~pymongo.HASHED`,
        :data:`~pymongo.TEXT`).

        To create a single key ascending index on the key ``'mike'`` we just
        use a string argument::

          >>> my_collection.create_index("mike")

        For a compound index on ``'mike'`` descending and ``'eliot'``
        ascending we need to use a list of tuples::

          >>> my_collection.create_index([("mike", pymongo.DESCENDING),
          ...                             ("eliot", pymongo.ASCENDING)])

        All optional index creation parameters should be passed as
        keyword arguments to this method. For example::

          >>> my_collection.create_index([("mike", pymongo.DESCENDING)],
          ...                            background=True)

        Valid options include, but are not limited to:

          - `name`: custom name to use for this index - if none is
            given, a name will be generated.
          - `unique`: if ``True`` creates a uniqueness constraint on the index.
          - `background`: if ``True`` this index should be created in the
            background.
          - `sparse`: if ``True``, omit from the index any documents that lack
            the indexed field.
          - `bucketSize`: for use with geoHaystack indexes.
            Number of documents to group together within a certain proximity
            to a given longitude and latitude.
          - `min`: minimum value for keys in a :data:`~pymongo.GEO2D`
            index.
          - `max`: maximum value for keys in a :data:`~pymongo.GEO2D`
            index.
          - `expireAfterSeconds`: <int> Used to create an expiring (TTL)
            collection. MongoDB will automatically delete documents from
            this collection after <int> seconds. The indexed field must
            be a UTC datetime or the data will not expire.
          - `partialFilterExpression`: A document that specifies a filter for
            a partial index.

        See the MongoDB documentation for a full list of supported options by
        server version.

        .. warning:: `dropDups` is not supported by MongoDB 3.0 or newer. The
          option is silently ignored by the server and unique index builds
          using the option will fail if a duplicate value is detected.

        .. note:: `partialFilterExpression` requires server version **>= 3.2**

        :Parameters:
          - `keys`: a single key or a list of (key, direction)
            pairs specifying the index to create
          - `**kwargs` (optional): any additional index creation
            options (see the above list) should be passed as keyword
            arguments

        .. versionchanged:: 3.2
            Added partialFilterExpression to support partial indexes.

        .. mongodoc:: indexes
        """
        keys = _index_list(keys)
        name = kwargs.setdefault('name', _gen_index_name(keys))

        index_doc = _index_document(keys)
        index = {'key': index_doc}
        index.update(kwargs)

        cmd = SON([('createIndexes', self.name), ('indexes', [index])])
        connection = self.database.client.get_connection()

        await connection.command(self.database.name, cmd, ReadPreference.PRIMARY, self.codec_options)
        return name

    async def drop_index(self, index_or_name):
        """Drops the specified index on this collection.

        Can be used on non-existant collections or collections with no
        indexes.  Raises OperationFailure on an error (e.g. trying to
        drop an index that does not exist). `index_or_name`
        can be either an index name (as returned by `create_index`),
        or an index specifier (as passed to `create_index`). An index
        specifier should be a list of (key, direction) pairs. Raises
        TypeError if index is not an instance of (str, unicode, list).

        .. warning::

          if a custom name was used on index creation (by
          passing the `name` parameter to :meth:`create_index` or
          :meth:`ensure_index`) the index **must** be dropped by name.

        :Parameters:
          - `index_or_name`: index (or name of index) to drop
        """
        name = index_or_name
        if isinstance(index_or_name, list):
            name = _gen_index_name(index_or_name)

        if not isinstance(name, str):
            raise TypeError('index_or_name must be an index name or list')

        cmd = SON([('dropIndexes', self.name), ('index', name)])
        connection = self.database.client.get_connection()
        await connection.command(
            self.database.name, cmd, ReadPreference.PRIMARY, self.codec_options,
            allowable_errors=['ns not found']
        )

    async def drop_indexes(self):
        """Drops all indexes on this collection.

        Can be used on non-existant collections or collections with no indexes.
        Raises OperationFailure on an error.
        """
        await self.drop_index('*')

    async def find(self, filter: Optional[dict] = None, projection: Optional[Union[dict, list]] = None,
                   skip: int = 0, limit: int = 0, sort: Optional[List[Tuple]]=None, modifiers: Optional[dict]=None,
                   batch_size: int=100) -> Cursor:
        connection = self.database.client.get_connection()

        return Cursor(connection, self, filter, projection, skip, limit, sort, modifiers, batch_size)

    async def find_one(self, filter: Optional[dict] = None, projection: Optional[Union[dict, list]] = None,
                       skip: int = 0, sort: Optional[List[Tuple]]=None, modifiers: Optional[dict]=None) -> Optional[dict]:
        if isinstance(filter, ObjectId):
            filter = {'_id': filter}

        result_cursor = await self.find(
            filter=filter, projection=projection, skip=skip, limit=1, sort=sort, modifiers=modifiers
        )
        result = None
        async for item in result_cursor:
            result = item

        return result

    async def insert_one(self, document: MutableMapping, bypass_document_validation: bool=False,
                         check_keys: bool=True) -> InsertOneResult:
        if '_id' not in document:
            document['_id'] = ObjectId()

        write_concern = self.write_concern.document
        acknowledged = write_concern.get('w') != 0

        connection = self.database.client.get_connection()

        if acknowledged:
            command = SON([('insert', self.name),
                           ('ordered', True),
                           ('documents', [document])])

            if bypass_document_validation:
                command['bypassDocumentValidation'] = True

            result = await connection.command(
                self.database.name, command, ReadPreference.PRIMARY, self.codec_options
            )

            _check_write_command_response([(0, result)])
        else:
            _, msg, _ = message.insert(
                str(self), [document], check_keys,
                acknowledged, write_concern, False, self.__write_response_codec_options
            )
            await connection.send_message(msg)

        return InsertOneResult(document['_id'], acknowledged)

    async def insert_many(self, documents: Iterable[dict], ordered: bool=True,
                          bypass_document_validation: bool=False) -> InsertManyResult:

        if not isinstance(documents, collections.Iterable) or not documents:
            raise TypeError("documents must be a non-empty list")

        blk = Bulk(self, ordered, bypass_document_validation)
        inserted_ids = []
        for document in documents:
            common.validate_is_document_type('document', document)
            if '_id' not in document:
                document['_id'] = ObjectId()
            blk.ops.append((message._INSERT, document))
            inserted_ids.append(document['_id'])

        write_concern = self.write_concern.document
        acknowledged = write_concern.get('w') != 0

        if acknowledged:
            await blk.execute(write_concern)
        else:
            connection = self.database.client.get_connection()
            _, msg, _ = message.insert(
                str(self), documents, False,
                acknowledged, write_concern, False, self.__write_response_codec_options
            )
            await connection.send_message(msg)

        return InsertManyResult(inserted_ids, self.write_concern.acknowledged)

    async def reindex(self):
        """Rebuilds all indexes on this collection.

        .. warning:: reindex blocks all other operations (indexes
           are built in the foreground) and will be slow for large
           collections.
        """
        cmd = SON([('reIndex', self.name)])

        connection = self.database.client.get_connection()

        await connection.command(self.database.name, cmd, ReadPreference.PRIMARY, self.codec_options)

    async def list_indexes(self):
        """Get a cursor over the index documents for this collection.

          >>> for index in db.test.list_indexes():
          ...     print(index)
          ...
          SON([(u'v', 1), (u'key', SON([(u'_id', 1)])),
               (u'name', u'_id_'), (u'ns', u'test.test')])

        :Returns:
          An instance of :class:`~pymongo.command_cursor.CommandCursor`.

        .. versionadded:: 3.0
        """
        codec_options = CodecOptions(SON)
        coll = self.with_options(codec_options)

        cmd = SON([('listIndexes', self.name), ('cursor', {})])

        connection = self.database.client.get_connection()

        cursor = (await connection.command(
            self.database.name, cmd, ReadPreference.PRIMARY, codec_options
        ))['cursor']

        return CommandCursor(connection, coll, cursor)

    def with_options(
            self, codec_options=None, read_preference=None,
            write_concern=None, read_concern=None):
        """Get a clone of this collection changing the specified settings.

          >>> coll1.read_preference
          Primary()
          >>> from pymongo import ReadPreference
          >>> coll2 = coll1.with_options(read_preference=ReadPreference.SECONDARY)
          >>> coll1.read_preference
          Primary()
          >>> coll2.read_preference
          Secondary(tag_sets=None)

        :Parameters:
          - `codec_options` (optional): An instance of
            :class:`~bson.codec_options.CodecOptions`. If ``None`` (the
            default) the :attr:`codec_options` of this :class:`Collection`
            is used.
          - `read_preference` (optional): The read preference to use. If
            ``None`` (the default) the :attr:`read_preference` of this
            :class:`Collection` is used. See :mod:`~pymongo.read_preferences`
            for options.
          - `write_concern` (optional): An instance of
            :class:`~pymongo.write_concern.WriteConcern`. If ``None`` (the
            default) the :attr:`write_concern` of this :class:`Collection`
            is used.
          - `read_concern` (optional): An instance of
            :class:`~pymongo.read_concern.ReadConcern`. If ``None`` (the
            default) the :attr:`read_concern` of this :class:`Collection`
            is used.
        """
        return Collection(self.database,
                          self.name,
                          read_preference or self.read_preference,
                          read_concern or self.read_concern,
                          codec_options or self.codec_options,
                          write_concern or self.write_concern)
