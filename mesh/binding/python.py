import sys
from imp import new_module
from inspect import getsource
from os.path import exists, join as joinpath
from types import ModuleType

from mesh.bundle import Bundle, Specification
from mesh.constants import *
from mesh.exceptions import *
from mesh.transport.base import Client
from mesh.util import StructureFormatter, get_package_data, import_object
from scheme.surrogate import surrogate

class ReadOnlyError(Exception):
    """..."""

class Attribute(object):
    """A model attribute."""

    def __init__(self, name, field):
        self.field = field
        self.name = name

    def __get__(self, instance, owner):
        if instance is not None:
            try:
                return instance._data[self.name]
            except KeyError:
                return None
        else:
            return self

    def __set__(self, instance, value):
        if isinstance(value, Model):
            value = value.id
        instance._data[self.name] = value

class CompositeIdentifier(object):
    """A model attribute for composite identifiers."""

    def __init__(self, name, keys):
        self.keys = keys
        self.name = name

    def __get__(self, instance, owner):
        if instance is not None:
            values = []
            for key in self.keys:
                value = instance._data.get(key)
                if value is not None:
                    values.append(value)
                else:
                    return None
            else:
                return ';'.join(values)
        else:
            return self

    def __set__(self, instance, value):
        values = value.split(';')
        for i, key in enumerate(self.keys):
            instance._data[key] = values[i]

class Query(object):
    """A resource query."""

    def __init__(self, model, **params):
        self.model = model
        self.params = params

    def __iter__(self):
        return iter(self._execute_query())

    def all(self):
        return self._execute_query()

    def one(self):
        return self._execute_query()[0]

    def _execute_query(self):
        model = self.model
        models = []
        for result in model._get_client().execute(model._resource, 'query', None, self.params or None):
            models.append(model(**result))
        return models

class Model(object):
    """A resource model."""

    query_class = Query
    repr_attrs = ('id', 'name', 'status', 'platform_id')

    def __init__(self, **params):
        self._data = {}
        for key, value in params.iteritems():
            if key in self._attributes:
                setattr(self, key, value)
            else:
                raise AttributeError(key)

    def __repr__(self):
        attrs = []
        for attr in self.repr_attrs:
            value = getattr(self, attr, None)
            if value is not None:
                attrs.append('%s=%r' % (attr, value))

        classname = type(self).__name__
        return '%s(%s)' % (classname, ', '.join(attrs))

    def construct_surrogate(self, implementation, **params):
        return surrogate.construct(implementation, self._data, **params)

    @classmethod
    def create(cls, **params):
        request = cls._resource['requests'].get('create')
        if not request:
            raise RuntimeError()

        model_params = {}
        for field, value in request['schema'].extract(params).iteritems():
            if field in cls._attributes:
                model_params[field] = value

        instance = cls(**model_params)
        return instance.save(request, **params)

    def destroy(self, quiet=False, **params):
        request = self._get_request('delete')
        if self.id is None:
            return self

        try:
            response = self._execute_request(request, params or None)
        except GoneError:
            if not quiet:
                raise
        else:
            return response.content

    @classmethod
    def execute(cls, request, data, subject=None):
        return cls._get_client().execute(cls._resource, request, subject, data)

    def extract_dict(self, attrs=None, exclude=None, drop_none=False, **extraction):
        if isinstance(attrs, basestring):
            attrs = attrs.split(' ')
        elif not attrs:
            attrs = self._data.keys()
        if isinstance(attrs, (tuple, list)):
            attrs = dict(zip(attrs, attrs))

        if exclude:
            if isinstance(exclude, basestring):
                exclude = exclude.split(' ')
            for attr in exclude:
                attrs.pop(attr, None)

        for attr, name in attrs.iteritems():
            value = self._data.get(attr)
            if not (drop_none and value is None):
                extraction[name] = value

        return extraction

    @classmethod
    def generate_model(cls, specification, resource, mixins):
        bases = [cls]
        if mixins:
            for mixin in mixins:
                bases.append(mixin)

        composite_key = resource.get('composite_key')
        namespace = {
            '_composite_key': composite_key,
            '_name': resource['name'],
            '_resource': resource,
            '_specification': specification,
        }

        attributes = namespace['_attributes'] = {}
        if composite_key:
            namespace['id'] = attributes['id'] = CompositeIdentifier('id', composite_key)

        for attr, field in resource['schema'].iteritems():
            if attr not in attributes:
                namespace[attr] = attributes[attr] = Attribute(attr, field)

        return type(str(resource['classname']), tuple(bases), namespace)

    @classmethod
    def get(cls, id, **params):
        if isinstance(id, (list, tuple)):
            attrs = {}
            for i, key in enumerate(self._composite_key):
                attrs[key] = id[i]
        else:
            attrs = {'id': id}

        return cls(**attrs).refresh(**params)

    def refresh(self, **params):
        request = self._get_request('get')
        if self.id is None:
            return self

        response = self._execute_request(request, params or None)
        self._update_model(response.content)
        return self

    def put(self, **params):
        request = self._get_request('put')
        return self.save(request, **params)

    @classmethod
    def query(cls, **params):
        return cls.query_class(cls, **params)

    def save(self, _request=None, **params):
        request = _request
        if not request:
            if self.id is not None:
                request = self._get_request('update')
            else:
                request = self._get_request('create')

        data = request['schema'].extract(self._data)
        if params:
            data.update(params)

        response = self._execute_request(request, data)
        self._update_model(response.content)
        return self

    def set(self, **params):
        for attr, value in params.iteritems():
            setattr(self, attr, value)
        return self

    def update(self, attrs, **params):
        self._update_model(attrs)
        return self.save(**params)

    def _execute_request(self, request, data=None):
        subject = None
        if request['specific']:
            subject = self.id
        return self._get_client().execute(self._resource, request['name'], subject, data)

    @classmethod
    def _get_client(cls):
        return Client.get_client(cls._specification)

    def _get_request(self, name):
        request = self._resource['requests'].get(name)
        if request:
            return request
        else:
            raise ValueError(name)

    def _update_model(self, data):
        if data:
            self._data.update(data)

class ResourceSet(dict):
    def __getattr__(self, name):
        try:
            return self[name]
        except KeyError:
            raise AttributeError(name)

def bind(binding, name, mixin_modules=None):
    if isinstance(binding, basestring):
        binding = import_object(binding)

    if isinstance(binding, ModuleType):
        binding = getattr(binding, 'binding', None)

    if isinstance(binding, Bundle):
        binding = binding.specify()

    if isinstance(binding, (Specification, dict)):
        binding = Binding(binding, mixin_modules)

    if not isinstance(binding, Binding):
        raise TypeError(binding)

    return binding.generate(name)

class Binding(object):
    """A python binding manager."""

    def __init__(self, specification, mixin_modules=None,
            mixin_classes=None, binding_module='mesh.standard.python'):

        if isinstance(specification, basestring):
            specification = import_object(specification)
        if isinstance(specification, Bundle):
            specification = specification.specify()
        elif not isinstance(specification, Specification):
            specification = Specification(specification)

        if isinstance(binding_module, basestring):
            binding_module = import_object(binding_module)

        self.cache = {}
        self.binding_module = binding_module
        self.mixins = {}
        self.specification = specification

        if mixin_classes:
            for mixin_class in mixin_classes:
                self._associate_mixin_class(mixin_class)
        if mixin_modules:
            self._enumerate_mixin_classes(mixin_modules)

    def __repr__(self):
        return 'Binding(%s)' % self.specification.name

    def generate(self, name):
        try:
            return self.cache[name]
        except KeyError:
            pass

        resource = self.specification.find(name)
        if '__subject__' in resource:
            target = self._generate_model(resource)
        else:
            target = ResourceSet()
            for candidate in resource.itervalues():
                if candidate['__subject__'] == 'resource':
                    target[candidate['classname']] = self._generate_model(candidate)

        self.cache[name] = target
        return target

    def _associate_mixin_class(self, mixin_class):
        try:
            targets = mixin_class.mixin
        except Exception:
            return

        mixins = self.mixins
        if isinstance(targets, basestring):
            targets = targets.split(' ')
        if isinstance(targets, (list, tuple)):
            for target in targets:
                if target in mixins:
                    mixins[target].append(mixin_class)
                else:
                    mixins[target] = [mixin_class]

    def _enumerate_mixin_classes(self, modules):
        if isinstance(modules, basestring):
            modules = modules.split(' ')

        for name in modules:
            module = import_object(name)
            for attr in dir(module):
                self._associate_mixin_class(getattr(module, attr))

    def _generate_model(self, resource):
        return self.binding_module.Model.generate_model(self.specification, resource,
            self.mixins.get(resource['classname']))

class BindingGenerator(object):
    """Generates python bindings for one or more mesh bundles.

    :param list mixin_modules: Optional, default is ``None``; a ``list`` of one or
        more mixin modules, specified as dotted package paths, to evaluate when
        generating bindings.

    :param str binding_module: Optional, default is ``mesh.standard.python``; the
        dotted package path of the module which should be used as the basis
        for any generated bindings.

    :param str specification_var: Optiona, default is ``specification``; the
        name which should be used for the bundle specification in the
        generated bindings.
    """

    CONSTRUCTOR_PARAMS = ('mixin_modules', 'binding_module')
    MODULE_TMPL = get_package_data('mesh.binding', 'templates/module.py.tmpl')

    def __init__(self, mixin_modules=None, binding_module='mesh.standard.python'):
        if isinstance(mixin_modules, basestring):
            mixin_modules = mixin_modules.split(' ')

        self.binding_module = binding_module
        self.formatter = StructureFormatter()
        self.mixin_modules = mixin_modules

    def generate(self, bundle):
        if isinstance(bundle, basestring):
            bundle = import_object(bundle)

        source = self._generate_binding(bundle)
        return '%s.py' % bundle.name, source

    def generate_dynamically(self, bundle):
        if isinstance(bundle, basestring):
            bundle = import_object(bundle)

        source = self._generate_binding(bundle)
        module = new_module(bundle.name)

        exec source in module.__dict__
        return module

    def _generate_binding(self, bundle):
        specification = self.formatter.format(bundle.describe())
        mixins, mixin_classes = self._generate_mixins()

        return self.MODULE_TMPL % {
            'binding_module': self.binding_module,
            'mixins': mixins,
            'mixin_classes': mixin_classes,
            'specification': specification,
        }

    def _generate_mixins(self):
        if not self.mixin_modules:
            return '', ''

        mixins = []
        mixin_classes = []

        for name in self.mixin_modules:
            module = import_object(name)
            for attr in dir(module):
                value = getattr(module, attr)
                try:
                    targets = value.mixin
                except Exception:
                    continue
                mixins.append(getsource(value))
                mixin_classes.append(attr)

        return '\n'.join(mixins), ', '.join(mixin_classes)

def generate_dynamic_binding(bundle, mixin_modules=None,
        binding_module='mesh.standard.python'):

    generator = BindingGenerator(mixin_modules, binding_module)
    return generator.generate_dynamically(bundle)

class BindingLoader(object):
    """Import loader for mesh bindings.

    When installed in ``sys.meta_path``, .mesh files will be dynamically converted
    to binding modules when imported.
    """

    def __init__(self, filename):
        self.filename = filename

    def __repr__(self):
        return 'BindingLoader(%r)' % self.filename

    @classmethod
    def find_module(cls, fullname, path=None):
        if path:
            path = path[0]
        else:
            return

        module = fullname.rpartition('.')[-1]
        if exists(joinpath(path, '%s.py' % module)):
            return

        filename = joinpath(path, '%s.mesh' % module)
        if exists(filename):
            return cls(filename)

    def load_module(self, fullname):
        namespace = {}
        execfile(self.filename, namespace)

        specification = namespace.get('bundle')
        if specification is None:
            specification = namespace.get('specification')
            if specification is None:
                raise ImportError(fullname)

        if fullname in sys.modules:
            module = sys.modules[fullname]
        else:
            module = sys.modules[fullname] = new_module(fullname)

        module.__file__ = self.filename
        module.__loader__ = self
        module.__package__ = fullname.rpartition('.')[0]

        module.binding = Binding(specification, namespace.get('mixins'))
        module.specification = module.binding.specification
        return module

def install_binding_loader():
    """Installs the mesh binding loader into ``sys.meta_path``, enabling the use
    of .mesh files, which will then be dynamically converted into binding modules
    upon import. This function can be called multiple times without error.
    """

    if BindingLoader not in sys.meta_path:
        sys.meta_path.insert(0, BindingLoader)
