"""Custom Django authentication backend"""
import abc

from boto3.exceptions import Boto3Error
from botocore.exceptions import ClientError
from django.conf import settings
from django.contrib.auth.backends import ModelBackend
from django.contrib.auth import get_user_model
from django.contrib.auth.models import Group
from django.utils.six import iteritems

from warrant import Cognito
from .utils import cognito_to_dict


class CognitoUser(Cognito):
    user_class = get_user_model()
    # Mapping of Cognito User attribute name to Django User attribute name
    COGNITO_ATTR_MAPPING = getattr(settings, 'COGNITO_ATTR_MAPPING',
                                   {
                                       'email': 'email',
                                       'given_name': 'first_name',
                                       'family_name': 'last_name',
                                   }
                                   )

    def get_user_obj(self,username=None,attribute_list=[],metadata={},attr_map={}):
        user_attrs = cognito_to_dict(attribute_list,CognitoUser.COGNITO_ATTR_MAPPING)
        django_fields = ['{}_id'.format(f.name) if f.is_relation else f.name
                         for f in CognitoUser.user_class._meta.get_fields()]
        extra_attrs = {}
        for k, v in user_attrs.items():
            if k not in django_fields:
                extra_attrs.update({k: user_attrs[k]})
        for k, v in extra_attrs.items():
            del user_attrs[k]
        for k, v in user_attrs.items():
            if k.endswith('_id'):
                user_attrs[k] = int(v)
        if getattr(settings, 'COGNITO_CREATE_UNKNOWN_USERS', True):
            is_staff = getattr(settings, 'COGNITO_ALLOW_BACKEND_ACCESS', False)
            user, created = CognitoUser.user_class.objects.update_or_create(
                username=username,
                is_staff=is_staff,
                defaults=user_attrs)
            aws_groups_response = self.client.admin_list_groups_for_user(
                    Username=username,
                    UserPoolId=settings.COGNITO_USER_POOL_ID)
            aws_groups_list = [entry['GroupName'] for entry in aws_groups_response['Groups']]
            groups = Group.objects.filter(name__in=aws_groups_list)
            for group in groups:
                user.groups.add(group)
        else:
            try:
                user = CognitoUser.user_class.objects.get(username=username)
                for k, v in iteritems(user_attrs):
                    setattr(user, k, v)
                user.save()
            except CognitoUser.user_class.DoesNotExist:
                    user = None
        if user:
            for k, v in extra_attrs.items():
                setattr(user, k, v)
        return user


class AbstractCognitoBackend(ModelBackend):
    __metaclass__ = abc.ABCMeta

    UNAUTHORIZED_ERROR_CODE = 'NotAuthorizedException'

    USER_NOT_FOUND_ERROR_CODE = 'UserNotFoundException'

    COGNITO_USER_CLASS = CognitoUser

    @abc.abstractmethod
    def authenticate(self, username=None, password=None):
        """
        Authenticate a Cognito User
        :param username: Cognito username
        :param password: Cognito password
        :return: returns User instance of AUTH_USER_MODEL or None
        """
        cognito_user = self.COGNITO_USER_CLASS(
            settings.COGNITO_USER_POOL_ID,
            settings.COGNITO_APP_ID,
            access_key=getattr(settings, 'AWS_ACCESS_KEY_ID', None),
            secret_key=getattr(settings, 'AWS_SECRET_ACCESS_KEY', None),
            username=username)
        try:
            cognito_user.authenticate(password)
        except (Boto3Error, ClientError) as e:
            return self.handle_error_response(e)
        user = cognito_user.get_user(self.COGNITO_USER_CLASS.COGNITO_ATTR_MAPPING)
        if user:
            user.access_token = cognito_user.access_token
            user.id_token = cognito_user.id_token
            user.refresh_token = cognito_user.refresh_token

        return user

    def handle_error_response(self, error):
        error_code = error.response['Error']['Code']
        if error_code in [
                AbstractCognitoBackend.UNAUTHORIZED_ERROR_CODE,
                AbstractCognitoBackend.USER_NOT_FOUND_ERROR_CODE
            ]:
            return None
        raise error


class CognitoBackend(AbstractCognitoBackend):
    def authenticate(self, request, username=None, password=None):
        """
        Authenticate a Cognito User and store an access, ID and
        refresh token in the session.
        """
        user = super(CognitoBackend, self).authenticate(
            username=username, password=password)
        if user:
            request.session['ACCESS_TOKEN'] = user.access_token
            request.session['ID_TOKEN'] = user.id_token
            request.session['REFRESH_TOKEN'] = user.refresh_token
            request.session.save()
        return user
