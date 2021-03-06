#!/usr/bin/env python
# -*- coding: utf-8 -*-

from contextlib import contextmanager, suppress
from operator import attrgetter
import os
from os.path import abspath, dirname, join, isfile

from .parser import parse, _units, Node, Include, Create, Template, Name
from .datatypes import Value, EnumType, FlagType, String, Number, Angle, AngleUnit, Time, TimeUnit, BinOp, UnaryOp, Call
from .elements import Element, Scene, Percentage, ElementError, ElementPropertyDefinedError, ElementPropertyReadonlyError, ElementPropertyConstantError, CircularReferenceError
from .functions import functions


_scenes_dir = abspath(join(dirname(__file__), os.pardir, "scenes"))
_ext = ".anim"


class SceneBuilderError(Exception):
	pass


class SceneBuilder:
	def __init__(self):
		self.templates = None
		self._elements = None
		self._types = None
		# TODO: Change this to an ordered set to avoid duplicates
		self.search_paths = [_scenes_dir]
		self._including = []
		self._included = set()

	@property
	def _element(self):
		return self._elements[-1]

	@contextmanager
	def _push_element(self, element):
		self._elements.append(element)
		yield
		assert self._elements.pop() is element

	@property
	def _type(self):
		return self._types[-1]

	@contextmanager
	def _push_type(self, type):
		self._types.append(type)
		yield
		assert self._types.pop() is type

	def _get_template(self, name, *, token=None):
		try:
			return self.templates[name]
		except KeyError:
			raise self._create_error(f"Creating undefined {name!r} template", token=token) from None

	def _get_element_type(self, name, *, token=None):
		template = self._get_template(name, token=token)
		if isinstance(template, Template):
			return self._get_element_type(template.inherit or "Drawable")
		assert issubclass(template, Element)
		return template

	def _apply_template(self, element, template, *, token=None):
		if isinstance(template, str):
			try:
				template = self.templates[template]
			except KeyError:
				raise self._create_error(f"Creating undefined {template!r} template", token=token) from None

		if isinstance(template, Template):
			with self._push_element(element):
				self._apply_template(element, template.inherit or "Drawable", token=token)

				for child in self._build_children(template):
					pass
		else:
			element.on_ready()

	def _get_property(self, element, name, *, token=None):
		assert isinstance(element, Element)

		while element is not None:
			with suppress(KeyError):
				return element.get(name)
			element = element.parent

		self._fail(f"Undefined property {name!r}", token=token)

	@staticmethod
	def _create_error(message, *, after=None, token=None):
		# TODO: Include filename in the error
		if token is not None:
			begin, end = token.span
			if after:
				return SceneBuilderError("%s at %d:%d to %d:%d\n%s" % (message, *begin, *end, after))
			else:
				return SceneBuilderError("%s at %d:%d to %d:%d" % (message, *begin, *end))
		elif after:
			return SceneBuilderError(f"{message}\n{after}")
		else:
			return SceneBuilderError(message)

	def _fail(self, message, *, after=None, token=None):
		raise self._create_error(message, after=after, token=token)

	def _find_scene_file(self, path):
		_path = path

		path = join(*path) + _ext
		for dirpath in reversed(self.search_paths):
			filename = join(dirpath, path)
			if isfile(filename):
				return filename

		paths = "\n".join(f"- {join(dirpath, path)}" for dirpath in reversed(self.search_paths))
		raise FileNotFoundError(f"Failed including {'.'.join(_path)}\nTried...\n{paths}")

	def _is_including(self):
		return len(self._including) > 0

	def _include(self, filename):
		filename = abspath(filename)

		if filename in self._including:
			return
		if filename in self._included:
			return

		self.search_paths.append(dirname(filename))

		self._including.append(filename)

		with open(filename) as f:
			string = f.read()

		self._build(parse(string))

		self._including.pop()
		self._included.add(filename)

		self.search_paths.pop()

	def build(self, string):
		if isinstance(string, str):
			return self.build(parse(string))
		else:
			assert isinstance(string, Create)
			assert string.element == "Scene"

			self.templates = dict((template.__name__, template) for template in Element.list_element_types())
			self._elements = []
			self._types = []

			scene = self._build(string)

			assert isinstance(scene, Scene)

			self._elements = None
			self._types = None

			return scene

	def _build(self, node):
		assert isinstance(node, Node)
		method = "_build_%s" % node.__class__.__name__
		visitor = getattr(self, method)
		return visitor(node)

	def _build_children(self, node):
		for child in node.children:
			yield self._build(child)

	def _build_Include(self, include):
		try:
			include_filename = self._find_scene_file(include.path)
		except FileNotFoundError as ex:
			message, after = str(ex).partition("\n")[0::2]
			raise self._create_error(message, after=after, token=include.token) from None

		self._include(include_filename)

	def _build_Scene(self, scene):
		if not self._is_including():
			return self._build_Create(scene)

		for child in scene.children:
			if isinstance(child, (Include, Template)):
				result = self._build(child)
				assert result is None

	def _build_Create(self, create):
		if create.name:
			raise NotImplementedError

		element_type = self._get_element_type(create.element, token=create.token)

		element = element_type()
		element.on_init()

		parent = None
		with suppress(IndexError):
			parent = self._element
		if parent is not None:
			try:
				parent.add(element)
				parent.on_element(element)
			except NotImplementedError:
				raise self._create_error(f"Cannot add {element.__class__.__name__} to {parent.__class__.__name__}", token=create.token) from None

		self._apply_template(element, create.element, token=create.token)

		with self._push_element(element):
			for child in self._build_children(create):
				pass

		try:
			element.on_created()
		except ElementError as ex:
			raise self._create_error(f"{ex} in {element.__class__.__name__}", token=create.token) from None

		return element

	def _build_Template(self, template):
		if template.name in self.templates:
			self._fail(f"Redeclaration of {template.name!r}", token=template.token)

		self.templates[template.name] = template

		return None

	def _build_Define(self, define):
		assert len(define.children) == 2

		name = define.name
		assert isinstance(name, Name)
		name = name.name

		value = self._build(define.value)

		assert isinstance(name, str)
		assert isinstance(value, Value)

		try:
			self._element.define(name, value)
		except ElementPropertyDefinedError as ex:
			raise self._create_error(f"{ex} in {self._element.__class__.__name__}", token=define.token) from None

		return None

	def _build_Assign(self, assign):
		assert len(assign.children) == 2

		name = assign.name
		assert isinstance(name, Name)
		name = name.name

		try:
			type = self._element.get(name).types[0]
		except KeyError:
			raise self._create_error(f"Assigning value to undefined property {name!r} in {self._element.__class__.__name__}", token=assign.token) from None

		with self._push_type(type):
			value = self._build(assign.value)

		assert isinstance(value, Value)

		try:
			self._element.set(name, value)
		# except KeyError:
		# 	raise self._create_error(f"Assigning value to undefined property {name!r} in {self._element.__class__.__name__}", token=assign.token) from None
		except TypeError as ex:
			raise self._create_error(f"{ex} in {self._element.__class__.__name__}", token=assign.token) from None
		# except ElementPropertyReadonlyError as ex:
		# 	raise self._create_error(f"{ex} in {self._element.__class__.__name__}", token=assign.token) from None
		except ElementPropertyReadonlyError:
			raise self._create_error(f"Cannot assign to readonly property {name!r} in {self._element.__class__.__name__}", token=assign.token) from None
		except ElementPropertyConstantError as ex:
			raise self._create_error(f"{ex} in {self._element.__class__.__name__}", token=assign.token) from None
		except CircularReferenceError as ex:
			paths = "\n".join(" -> ".join(map(attrgetter("name"), path)) for path in ex.paths)
			raise self._create_error(f"{ex} in {self._element.__class__.__name__}", after=f"Paths:\n{paths}", token=assign.token) from None

		return None

	def _build_MemberAccess(self, member_access):
		value = self._build(member_access.value)
		member = member_access.member

		value = value.eval()

		assert isinstance(value, Element)
		assert isinstance(member, Name)

		return self._get_property(value, member.name, token=member_access.token)

	def _build_UnaryOp(self, unary_op):
		assert len(unary_op.children) == 1
		operand, = self._build_children(unary_op)
		return UnaryOp(unary_op.op, operand)

	def _build_BinOp(self, bin_op):
		assert len(bin_op.children) == 2
		lhs, rhs = self._build_children(bin_op)
		return BinOp(bin_op.op, lhs, rhs)

	def _build_Number(self, number):
		assert len(number.children) == 0

		value, unit = number.value, number.unit

		if unit is None:
			return Number(value)
		elif unit == "%":
			return Percentage(value)
		elif unit in (unit.value for unit in AngleUnit):
			return Angle(value, AngleUnit(unit))
		elif unit in (unit.value for unit in TimeUnit):
			return Time(value, TimeUnit(unit))
		else:
			self._fail(f"Unexpected unit {unit!r}, expected any of {_units}", token=number.token)

		return number

	def _build_String(self, string):
		assert len(string.children) == 0
		return String(string.string)

	def _build_Call(self, call):
		args = tuple(self._build_children(call))
		return Call(functions[call.name], args)

	def _build_Name(self, name):
		assert len(name.children) == 0

		with suppress(IndexError, KeyError):
			if isinstance(self._type, (EnumType, FlagType)):
				return self._type.enum[name.name].box()

		return self._get_property(self._element, name.name, token=name.token)
