"""
DataForms System
================

See the GettingStarted guide at:
http://code.google.com/p/django-dataforms/wiki/GettingStarted
"""

# Load the user's custom validation, if it exists
try: import validation
except ImportError: validation = None

from collections import defaultdict
from django import forms
from django.forms.forms import BoundField
from django.utils import simplejson as json
from django.utils.datastructures import SortedDict
from django.core.exceptions import FieldError
from django.template.defaultfilters import safe, force_escape

from .settings import (FIELD_MAPPINGS, SINGLE_CHOICE_FIELDS, MULTI_CHOICE_FIELDS, CHOICE_FIELDS,
				FIELD_DELIMITER, SINGLE_NUMBER_FIELDS, MULTI_NUMBER_FIELDS, NUMBER_FIELDS, HIDDEN_BINDINGS_SLUG)
from .models import DataForm, Collection, Field, FieldChoice, Choice, Answer, Submission, AnswerChoice, AnswerText, AnswerNumber, CollectionDataForm, Binding, Section

class BaseDataForm(forms.BaseForm):
	def __init__(self, *args, **kwargs):
		super(BaseDataForm, self).__init__(*args, **kwargs)
		self.__generate_bound_fields()
	
	def __generate_bound_fields(self):
		self.bound_fields = SortedDict([(name, BoundField(self, field, name)) for name, field in self.fields.items()])
	
	def __iter__(self):
		"""
		Overload of the BaseForm iteration to maintain a persistent set of bound_fields.
		This allows us to inject attributes in to the fields and these attributes will
		persist the next time that the form is iterated over.
		"""
		
		for name in self.bound_fields:
			yield self.bound_fields[name]
	
	def is_valid(self):
		# Delete the hidden bindings field before we process, because it should never be handled
		_remove_bindings_field(self)
		return super(BaseDataForm, self).is_valid()
	
	def save(self):
		"""	
		Saves the validated, cleaned form data. If a submission already exists,
		the new data will be merged over the old data.
		"""
		
		# TODO: think about adding an "overwrite" argument to this function, default of False,
		# which will determine if an error should be thrown if the submission object already
		# exists, or if we should trust the data and overwrite the previous submission.

		# FIXME: This is probably ridiculously inefficient and may generate a ton of SQL.
		# Do some profiling and see if there are ways to batch some of the SQL queries together.
		#
		# Specifically:
		#  * All the get_or_create() functions could become creates if we delete all answers
		#	that we are about to update.
		#  * Is there any way to batch INSERTs in Django's ORM?
		
		# FIXME: check if cleaned_data exists and throw an error informing the user
		# they need to call is_valid() before calling this save function.
		
		if not hasattr(self, "submission"):
			# This needs to be get_or_create (not just create) for form collections
			# IE, the first form saved in a collection will create the submission
			# but all other forms will still not have self.submission
			self.submission, was_created = Submission.objects.get_or_create(slug=self.submission_slug)
		
		for key in self.fields.keys():
			# Mangle the key into the DB form, then get the right Field
			field = Field.objects.get(slug=_field_for_db(key))
			
			answer, was_created = Answer.objects.get_or_create(
				submission=self.submission,
				field=field,
				data_form=DataForm.objects.get(slug=self.slug),
			)
			
			if field.field_type in CHOICE_FIELDS:
				# STORAGE MODEL: AnswerChoice
				
				# Delete all previous choices
				# See note below about this deletion
				for answer_choice in AnswerChoice.objects.filter(answer=answer):
					answer_choice.delete()
				
				# If string, wrap as a list because the for-loop below assumes a list
				if isinstance(self.cleaned_data[key], str) or isinstance(self.cleaned_data[key], unicode):
					self.cleaned_data[key] = [self.cleaned_data[key]]
				
				# Add the selected choices
				choices = Choice.objects.filter(value__in=self.cleaned_data[key])
				for choice in choices:
					answer.answerchoice_set.create(choice=choice)
			elif field.field_type in SINGLE_NUMBER_FIELDS:
				# STORAGE MODEL: AnswerNumber
				# Pseudo-foreign key storage
				
				assert len(self.cleaned_data[key]) == 1
				
				# Delete all previous numbers
				# See note below about this deletion
				for answer_num in AnswerNumber.objects.filter(answer=answer):
					answer_num.delete()
				
				answer.answernumber_set.create(number=self.cleaned_data[key][0])
				
			elif field.field_type in MULTI_NUMBER_FIELDS:
				# STORAGE MODEL: AnswerNumber
				# Pseudo-many-to-many storage
				
				# Delete all previous numbers
				# RelatedManagers do not have a clear() method to delete all many-to-many
				# relations, so we need to do something different.
				#
				# This does not work (but is also silent):
				# answer.answernumber_set = []
				#
				# So, how about this. Is there a better way to do it though?
				for answer_num in AnswerNumber.objects.filter(answer=answer):
					answer_num.delete()
				
				for num in self.cleaned_data[key]:
					answer.answernumber_set.create(number=num)
			else:
				# STORAGE MODEL: AnswerText
				# Single answer with text content
				
				# Leave this conditional check here. It makes single checkboxes work. 
				content = self.cleaned_data[key] if self.cleaned_data[key] else ''
				
				if was_created:
					# Create new answer text
					answer.answertext_set.create(text=content)
				else:
					# Update old text answer
					try:
						answer_text = answer.answertext_set.get()
					except AnswerText.DoesNotExist:
						# This shouldn't happen, there should always be AnswerText when
						# the answer exists, but we might as well fix it and not error.
						answer_text = answer.answertext_set.create(text=content)
					
					if answer_text.text != content:
						answer_text.text = content
						answer_text.save() 

class BaseCollection(object):
	"""
	You shouldn't need to instantiate this object directly, use create_collection.
	
	When you have a collection, here are some tips:: 
	
		# You can see what's next and what came before
		collection.current_section
		collection.next_section
		collection.prev_section
	"""
		
	def __init__(self, title, description, slug, forms, sections):
		self.title = str(title)
		self.description = str(description)
		self.slug = str(slug)
		self.forms = forms
		
		# Section helpers
		self.sections = sections
		# Set all forms to be viewable initially
		self.set_section()
		
	def set_section(self, section=None):
		"""
		Set the visible section whose forms will be returned
		when using array indexing.
		"""
		
		if isinstance(section, Section):
			section = section.slug

		if section is None:
			self.__form_existence = [True for form in self.forms]
		else:
			self.__form_existence = [True if form.section == section else False for form in self.forms]
			
		if True not in self.__form_existence:
			raise SectionDoesNotExist(section)
		
		# Set the indexes
		self._section = [row.slug for row in self.sections].index(section) if section else 0
		self._next_section = self._section+1 if self._section+1 < len(self.sections) else None
		self._prev_section = self._section-1 if self._section-1 >= 0 else None
		
		# Set the objects
		self.section = self.sections[self._section]
		self.next_section = self.sections[self._next_section] if self._next_section is not None else None
		self.prev_section = self.sections[self._prev_section] if self._prev_section is not None else None
		
	def __getitem__(self, arg):
		"""
		Usage::
			# Returns just the specified form
			collection[2]
		"""
		
		# The true index to the form the user is asking for is the normal index
		# into self.forms, but excluding forms masked out by __form_existence[] == False
		fake_index = -1
		for i in range(0, len(self.forms)+1):
			if self.__form_existence[i]:
				fake_index +=1
			if fake_index == arg:
				return self.forms[i]
				
	def __getslice__(self, start, end):
		"""
		Make a new collection with the given subset of forms
		"""
		
		
		return BaseCollection(
			title=self.title,
			description=self.description,
			slug=self.slug,
			forms=self.forms[start, end]
		)
	
	def __len__(self):
		"""
		:return: the number of contained forms (that are visible)
		"""
		
		return len([truth for truth in self.__form_existence if truth])
		
	def save(self):
		"""
		Save all contained forms
		"""
		
		for form in self:
			form.save()
		
	def is_valid(self):
		"""
		Validate all contained forms
		"""
		
		for form in self:
			if not form.is_valid():
				return False
		return True

def _remove_bindings_field(form):
	"""
	Delete the hidden bindings field before we process, because it should never be handled
	"""
	
	hidden_bindings_field_slug = _field_for_form(name=HIDDEN_BINDINGS_SLUG, form=form.meta['slug'])
	if form.fields.has_key(hidden_bindings_field_slug):
		del form.fields[hidden_bindings_field_slug]

def create_collection(request, collection, submission, readonly=False):
	"""
	Based on a form collection slug, create a list of form objects.
	
	:param request: the current page request object, so we can pull POST and other vars.
	:param collection: a data form collection slug or object
	:param submission: create-on-use submission slug or object; passed in to retrieve
		Answers from an existing Submission, or to be the slug for a new Submission.
	:return: a BaseCollection object, populated with the correct data forms and data
	"""
	
	# Slightly evil, do type checking to see if submission is a Submission object or string
	if isinstance(collection, str) or isinstance(collection, unicode):
		# Get the queryset for the form collection to pass in our dictionary
		try:
			collection = Collection.objects.get(visible=True, slug=collection)
		except Collection.DoesNotExist:
			raise Collection.DoesNotExist('''Collection %s does not exist. Make sure the slug name is correct and the collection is visible.''' % collection)
	
	# Get queryset for all the forms that are needed
	try:
		forms = DataForm.objects.filter(
				collectiondataform__collection=collection,
				collectiondataform__collection__visible=True
			).order_by('collectiondataform__order')
	except DataForm.DoesNotExist:
		raise DataForm.DoesNotExist('''Data Forms for collection %s	do not exist. Make sure the slug name is correct and the forms are visible.''' % collection)
	
	collection_bridge = CollectionDataForm.objects.filter(
		collection=collection,
		data_form__in=forms,
	)
	
	# Get the sections from the many-to-many, and then make the elements unique (a set)
	non_unique_sections = Section.objects.order_by("collectiondataform__order").filter(collectiondataform__collection=collection).distinct()

	# Force the query to evaluate
	non_unique_sections = list(non_unique_sections)

	# OK, this is evil. We have to manually remove duplicates that exist in the Section queryset.
	# See here for why mixing order_by and distinct returns duplicates.
	#
	# http://docs.djangoproject.com/en/dev/ref/models/querysets/#distinct
	#
	# Also, using list(set(non_unique_sections)) does not work, unfortunately.
	sections = []
	for section in non_unique_sections:
		if section not in sections:
			sections.append(section)
	
	# Initialize a list to contain all the form classes
	form_list = []
	
	# Populate the list
	for form in forms:
		# Hmm...is this evil?
		section = collection_bridge.get(data_form=form).section.slug
		
		form_list.append(create_form(request, form=form, submission=submission, section=section, readonly=readonly))
	
	# Pass our collection info and our form list to the dictionary
	collection = BaseCollection(
		title=collection.title,
		description=collection.description,
		slug=collection.slug,
		forms=form_list,
		sections=sections
	)
	
	return collection

def create_form(request, form, submission, title=None, description=None, section=None, readonly=False):
	"""
	Instantiate and return a dynamic form object, optionally already populated from an
	already submitted form.
	
	Usage::
	
		# Get a dynamic form. If a Submission with slug "myForm" exists,
		# this will return a bound form. Otherwise, it will be unbound.
		create_form(request, form="personal-info", submission="myForm")
		
		# Create a bound form to a previous submission object
		create_form(request, slug="personal-info", submission=Submission.objects.get(...))

	:param request: the current page request object, so we can pull POST and other vars.
	:param form: a data form slug or object
	:param submission: create-on-use submission slug or object; passed in to retrieve
		Answers from an existing Submission, or to be the slug for a new Submission.
	:param title: optional title; pulled from DB by default
	:param description: optional description; pulled from DB by default
	:param section: optional section; will be added as an attr to the form instance 
	"""
	
	# Slightly evil, do type checking to see if submission is a Submission object or string
	if isinstance(submission, str) or isinstance(submission, unicode):
		submission_slug = submission
		
		try:
			submission = Submission.objects.get(slug=submission)
		except Submission.DoesNotExist:
			submission = None
	else:
		submission_slug = submission.slug
	
	# Before we populate from submitted data, prepare the answers for insertion into the form
	data = get_answers(submission=submission, for_form=True)
	
	# Create our form class
	FormClass = _create_form(form=form, title=title, description=description, readonly=readonly)
	
	# Create the actual form instance, from the dynamic form class
	if request.method == 'POST':
		# We assume here that we don't need to overlay the POST data on top of the database
		# data because the initial form, before POST, will contain the database defaults and so
		# the resulting POST data will (in normal cases) originate from database defaults already.
		
		# This creates a bound form object.
		form = FormClass(data=request.POST, files=request.FILES)
	else:
		# We populate the initial data of the form from the database answers. Any questions we
		# don't have answers for in the database will use their initial field defaults.
		
		# This creates an unbound form object.
		form = FormClass(initial=(data if data else None))
		
	# Now that we have an instantiated form object, let's add our custom attributes
	if submission:
		form.submission = submission
	if submission_slug:
		form.submission_slug = submission_slug
		
	form.section = section
	
	return form

def _create_form(form, title=None, description=None, readonly=False):
	"""
	Creates a form class object.

	Usage::
	
		FormClass = _create_form(dataform="myForm")
		form = FormClass(data=request.POST)
	
	:param form: a data form slug or object
	:param title: optional title; pulled from DB by default
	:param description: optional description; pulled from DB by default
	"""
	
	meta = {}
	slug = form if isinstance(form, str) or isinstance(form, unicode) else form.slug
	final_fields = SortedDict()
	choices_dict = defaultdict(tuple)
	
	# Parse the slug and create a class title
	form_class_title = create_form_class_title(slug)
	
	# Make sure the form definition exists before continuing
	try:
		form = DataForm.objects.get(visible=True, slug=slug)
	except DataForm.DoesNotExist:
		raise DataForm.DoesNotExist('DataForm %s does not exist. Make sure the slug name is correct and the form is visible.' % slug)
	
	# Set the title and/or the description from the DB (but only if it wasn't given)
	meta['title'] = safe(form.title if not title else title)
	meta['description'] = safe(form.description if not description else description)
	meta['slug'] = form.slug
		
	# Get all the fields
	try:
		field_qs = Field.objects.filter(
			dataformfield__data_form__slug=slug,
			visible=True
		).order_by('dataformfield__order')
	except Field.DoesNotExist:
		# FIXME: is this error ever going to be raised? Won't it just be [] and not raise an error?
		raise Field.DoesNotExist('Field for %s do not exist. Make sure the slug name is correct and the fields are visible.' % slug)
	
	# Get all the choices associated to fields
	choices_qs = (
		FieldChoice.objects.select_related('choice', 'field').filter(
			field__dataformfield__data_form__slug=slug,
			field__visible=True
		).order_by('order')
	)

	fields = list(field_qs.values())
	
	# We originally were setting the "rel" attr on the field objects to do the bindings,
	# but this only works for checkboxes and other top-level field types. It does not work
	# with sub-options (like elements of a drop down) because Django does not yet support
	# (as of v1.0) extra attributes on <option> elements on Select widgets. So, instead:
	
	# Get the bindings for use in the Field Loop
	bindings = get_bindings(form=form)
	
	# Add a hidden field used for passing information to the JavaScript bindings function
	fields.append({
		'field_type': 'HiddenInput',
		'slug': HIDDEN_BINDINGS_SLUG,
		'initial': safe(force_escape(json.dumps(bindings))),
	})
	
	# Populate our choices dictionary
	for row in choices_qs:
		choices_dict[row.field.pk] += (row.choice.value, row.choice.title),
		
	# ----- Field Loop -----
	# Populate our fields dictionary for this form
	for row in fields:
		form_field_name = _field_for_form(name=row['slug'], form=slug)
		
		field_kwargs = {}
		field_attrs = {}
		
		if row.has_key('label'):
			field_kwargs['label'] = safe(row['label'])
		if row.has_key('help_text'):
			field_kwargs['help_text'] = safe(row['help_text'])
		if row.has_key('initial'):
			field_kwargs['initial'] = row['initial']
		if row.has_key('required'):
			field_kwargs['required'] = row['required']
			
		additional_field_kwargs = {}
		if row.has_key('arguments') and row['arguments'].strip():
			# Parse any additional field arguments as JSON and include them in field_kwargs
			temp_args = json.loads(str(row['arguments']))
			for arg in temp_args:
				additional_field_kwargs[str(arg)] = temp_args[arg]
		
		# Update the field arguments with the "additional arguments" JSON in the DB
		field_kwargs.update(additional_field_kwargs)
		
		field_map = FIELD_MAPPINGS[row['field_type']]
#		
#		# Set the rel attribute if this field is bound to a parent so the JavaScript can know the relation
#		if bindings.has_key(form_field_name):
#			field_attrs['rel'] = "id_%s" % bindings[form_field_name]
#		
		if readonly:
			field_attrs['readonly'] = "readonly"

		# Get the choices for single and multiple choice fields 
		if row['field_type'] in CHOICE_FIELDS:
			choices = ()

			# We add a separator for select boxes
			if row['field_type'] == 'Select':
				choices += ('', '--------'),
			
			# Populate our choices tuple
			choices += choices_dict[row['id']]
			field_kwargs['choices'] = choices
			
			if row['field_type'] in MULTI_CHOICE_FIELDS:
				# Get all of the specified default selected values (as a list, even if one element)
				field_kwargs['initial'] = (
					field_kwargs['initial'].split(',')
					if ',' in field_kwargs['initial']
					else [field_kwargs['initial'],]
				)
				# Remove whitespace so the user can use spaces
				field_kwargs['initial'] = [element.strip() for element in field_kwargs['initial']]
			
			if readonly:
				field_attrs['disabled'] = "disabled"
				
		if readonly and row['field_type'] == "CheckboxInput":
				field_attrs['disabled'] = "disabled"
			
		# Instantiate the widget that this field will use
		if field_map.has_key('widget'):
			field_kwargs['widget'] = field_map['widget'](attrs=field_attrs, **field_map['widget_kwargs'])
		
		# Add this field, including any widgets and additional arguments
		# (initial, label, required, help_text, etc)
		final_fields[form_field_name] = field_map['class'](**field_kwargs)
			
	attrs = {
		'declared_fields' : final_fields,
		'base_fields' : final_fields,
		'meta' : meta,
		'slug' : slug,
	}
	
	# Grab the dynamic validation function from validation.py
	if validation:
		validate = getattr(validation, form_class_title)

		# Pull the "clean_" functions from the validation
		# for this form and inject them into the form object
		for attr_name in dir(validate):
			if attr_name.startswith('clean'):
				attrs[attr_name] = getattr(validate, attr_name)
	
	# Return a class object of this form with all attributes
	DataFormClass = type(form_class_title, (BaseDataForm,), attrs)
	
	return DataFormClass

def get_answers(submission, for_form=False):
	"""
	Get the answers for a submission.

	This function intentionally does not return the answers in the same
	form as request.POST data will have submitted them (ie, every element
	wrapped as a list). This is because this function is meant to provide
	data that can be instantly consumed by some `FormClass(data=data)`
	instantiation, as done by create_form.

	:param submission: A Submission object or slug
	:param for_form: whether or not these answers should be made unique for
		use on a form, ie. if every field slug should be prepended with
		the form's slug. This can be annoying when just wanting to inspect
		answers from a submission, so it is set to False by default, but	needs
		to be True when used the keys will be used as form element names. 
	:return: a dictionary of answers
	"""

	data = defaultdict(list)

	# Slightly evil, do type checking to see if submission is a Submission object or string
	if isinstance(submission, str) or isinstance(submission, unicode):
		submission = Submission.objects.get(slug=submission).id
	if isinstance(submission, Submission):
		submission = submission.id

	# FIXME: Is this selected related working?
	# ANSWER:  NO!  It is not!  28+ queries per execution!
	answers = Answer.objects.get_answer_data(submission)

	# For every answer, do some magic and get it into our data dictionary
	for answer in answers:
		# Refactors the answer field name to be globally unique (so
		# that a field can be in multiple forms in the same POST)
		if for_form:
			answer_key = _field_for_form(name=str(answer['field_slug']), form=answer['dataform_slug'])
		else:
			answer_key = str(answer['field_slug'])

		# If there isn't an answer (answer['content'] is [None, None...]) then don't include
		if answer['content'] and True not in [(True if item is not None else False) for item in answer['content']]:
			continue

		if answer['field_type'] in MULTI_CHOICE_FIELDS + MULTI_NUMBER_FIELDS:
			data[answer_key] = answer['content']
		else:
			# SINGLE_CHOICE_FIELDS + SINGLE_NUMBER_FIELDS + text fields
			data[answer_key] = answer['content'][0]

	return dict(data)

def get_bindings(form):
	"""
	Get the bindings for specific submission
	
	:return: a dictionary of child_field_name-->parent_bound_field_name
	"""
	
	bindings = Binding.objects.select_related('parent_field__slug').filter(data_form=form)
	
	data = []
	
	for binding in bindings:
		
		child = _field_for_form(name=binding.child.slug, form=form.slug)
		
		if binding.parent_field:
			# We're bound to a parent field itself (like a checkbox)
			parent = _field_for_form(name=binding.parent_field.slug, form=form.slug)
			data.append((parent, child))
		elif binding.parent_choice:
			# We're bound to a specific choice in some field (like a <option> in select)
			parent = _field_for_form(name=binding.parent_choice.field.slug, form=form.slug)
			parent_choice = binding.parent_choice.choice.value
			data.append((parent, parent_choice, child))
		else:
			raise FieldError("One of { parent_field, parent_choice } is required on every Binding object.")
		
	return data

def create_form_class_title(slug):
	"""
	Transform "my-form-name" into "MyFormName"
	This is important because we need each form class to have a unique name.

	:param slug: the form slug from the DB
	"""

	return ''.join([word.capitalize() for word in str(slug).split('-')] + ['Form'])

def _field_for_form(name, form):
	"""
	Make a form field globally unique by prepending the form name
	field-name --> form-name--field-name
	"""
	return "%s%s%s" % (form, FIELD_DELIMITER, name)

def _field_for_db(name, packed_return=False):
	"""
	Take a form from POST data and get it back to its DB-capable field and form name
	"id_form-name--field-name" --> "field-name"
	"id_form-name--field-name" --> "form-name", "field-name"
	
	:arg packed_return: whether or not to return a tuple of
		(form_name, field_name), or just the field_name
	"""
	
	names = name.split(FIELD_DELIMITER)
	
	if packed_return:
		return (names[0][len("id_") if "id_" in names[0] else 0:], names[1])
	else:
		return names[1]

# Custom dataform exception classes
class RequiredArgument(Exception):
	pass

class SectionDoesNotExist(Exception):
	pass
