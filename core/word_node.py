#!/usr/bin/env python3
# Copyright 2016 Andy Chu. All rights reserved.
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#   http://www.apache.org/licenses/LICENSE-2.0
"""
word_node.py -- AST Nodes for the word language.

In contrast to the "dumb" nodes for the arith, bool, and command languages,
these nodes have a lot of behavior defined by virtual dispatch, precisely
BECAUSE the other 3 languages use the words as "tokens".
"""

import io
import re

from core.base import _Node
from core.id_kind import Id, Kind, IdName, LookupKind
from core.tokens import EncodeTokenVal
from core.value import Value

from core import util

#
# Word
#

# http://stackoverflow.com/questions/4419704/differences-between-declare-typeset-and-local-variable-in-bash
# "local and declare are mostly identical and take all the same arguments with
# two exceptions: local will fail if not used within a function, and local with
# no args filters output to print(only locals, declare doesn't.")
EAssignFlags = util.Enum('EAssignFlags', 'EXPORT READONLY'.split())
# I think I need scope and flags
EAssignScope = util.Enum('EAssignScope', 'LOCAL GLOBAL'.split())


#
# WordPart
#


class WordPart(_Node):
  def __init__(self, id):
    _Node.__init__(self, id)

  def __repr__(self):
    # repr() always prints as a single line
    f = io.StringIO()
    self.PrintLine(f)
    return f.getvalue()

  def PrintTree(self, f):
    raise NotImplementedError

  def PrintLine(self, f):
    # Circular -- each part must override at least one
    f.write(repr(self))

  def TokenPair(self):
    """
    Returns:
      Leftmost token and rightmost token.
    """
    raise NotImplementedError(self.__class__.__name__)

  def EvalStatic(self):
    """Evaluate a word at PARSE TIME.

    Used for here doc delimiters, function names, and for loop variable names.

    The first step is to follow the normal rules of parsing, but then we
    disallow var sub, command sub, arith sub, etc.

    Returns:
      3-tuple of
        ok: bool, success
        value: a string (not Value)
        quoted: whether any part of the word was quoted
    """
    raise NotImplementedError

  def VarLikeName(self):
    """Return the var name string, or False."""
    return False

  def ArithVarLikeName(self):
    """Return the var name string, or False.

    NOTE: This can't be combined with the above because the tokens have to be
    different.  Otherwise _ReadCompoundWord will be confused between array
    assigments foo=(1 2) and function calls foo(1, 2).
    """
    return False

  def UnquotedLiteralValue(self):
    """
    Returns a StringPiece value if it's a literal token, otherwise the empty
    string.
    Used only for Tilde detection.  TODO: Might want to reconsider that.
    """
    return ""

  def TestLiteralForSlash(self):
    """
    Returns:
      -2  : Not a literal part
      -1  : It is a literal, but no slash
       0- : position of the slash that is found

    Used for tilde expansion.
    """
    return -2

  def LiteralId(self):
    """
    If the WordPart consists of a single literal token, return its Id.  Used
    for Id.KW_For, or Id.RBrace, etc.
    """
    return Id.Undefined_Tok  # unequal to any other Id

  def IsSubst(self):
    """
    Returns:
      Is the part an substitution?  (If called
      This is used:

      1) To determine whether result of evaluation of the part should be split
      in a unquoted context.
      2) To determine whether an empty string can be elided.
      3) To do globbing.  If we are NOT in a substitution or literal.
    """
    return False


class ArrayLiteralPart(WordPart):
  """An Array literal is WordPart that contains other Words.

  In contrast, a DoubleQuotedPart is a WordPart that contains other
  WordParts.

  It's a WordPart because foo=(a b c) is a word with 2 parts.

  Note that foo=( $(ls /) ) is also valid.
  """
  def __init__(self):
    # There is no Left ArrayLiteral, so just use the right one.
    WordPart.__init__(self, Id.Right_ArrayLiteral)
    self.words = []  # type: List[CompoundWord]

  def __repr__(self):
    return '[Array ' + ' '.join(repr(w) for w in self.words) + ']'


class _LiteralPartBase(WordPart):
  def __init__(self, id, token):
    _Node.__init__(self, id)
    self.token = token

  def TokenPair(self):
    return self.token, self.token

  def __eq__(self, other):
    return self.token == other.token


class LiteralPart(_LiteralPartBase):
  """A word part written literally in the program text.

  It could be unquoted or quoted, depending on if it appears in a
  DoubleQuotedPart.  (SingleQuotedPart contains a list of Token instance, not
  WordPart instances.)
  """
  def __init__(self, token):
    _LiteralPartBase.__init__(self, Id.Lit_Chars, token)

  def __repr__(self):
    # This looks like a token, except it uses [] instead of <>.  We need the
    # exact type to reverse it, e.g. '"' vs \".

    # e.g. for here docs, break it for readability.  TODO: Might want the
    # PrintLine/PrintTree distinction for parts to.
    newline = ''
    #if self.token.val == '\n':
    #  newline = '\n'
    # TODO: maybe if we have the token number, we can leave out the type.  The
    # client can look it up?
    return '[%s %s]%s' % (
        IdName(self.token.id), EncodeTokenVal(self.token.val),
        newline)

  def EvalStatic(self):
    return True, self.token.val, False

  def VarLikeName(self):
    if self.token.id == Id.Lit_VarLike:
      assert self.token.val.endswith('=')
      return self.token.val[:-1]
    else:
      return False

  def ArithVarLikeName(self):
    if self.token.id == Id.Lit_ArithVarLike:
      return self.token.val
    else:
      return False

  def LiteralId(self):
    return self.token.id

  def UnquotedLiteralValue(self):
    return self.token.val

  def TestLiteralForSlash(self):
    return self.token.val.find('/')

  def SplitAtIndex(self, i):
    s = self.token.val
    return s[:i], s[i:]


class EscapedLiteralPart(_LiteralPartBase):
  """e.g. \* or \$."""

  def __init__(self, token):
    _LiteralPartBase.__init__(self, Id.Lit_EscapedChar, token)

  def __repr__(self):
    # Quoted part.  TODO: Get rid of \ ?
    return '[\ %s %s]' % (
        IdName(self.token.id), EncodeTokenVal(self.token.val))

  def EvalStatic(self):
    # I guess escaped literal is fine, like \E ?
    return True, self.token.val[1:], True

  # VarLikeName, TestLiteralForSlash, LiteralId: default values.  SplitAtIndex?
  # Only exists on regular LiteralPart


class SingleQuotedPart(WordPart):

  def __init__(self):
    WordPart.__init__(self, Id.Left_SingleQuote)
    self.tokens = []  # list of Id.Lit_Chars tokens

  def TokenPair(self):
    if self.tokens:
      return self.tokens[0], self.tokens[-1]
    else:
      # NOTE: This can't happen with ''.  TODO: Include the actual single
      # quote!
      return None, None

  def __repr__(self):
    return '[SQ %s]' % (' '.join(repr(t) for t in self.tokens))

  def _Eval(self):
    """Shared between Eval and EvalStatic."""
    return ''.join(t.val for t in self.tokens)

  def EvalStatic(self):
    # Single quoted literal can be here delimiter!  Like 'EOF'.
    return True, self._Eval(), True


class DoubleQuotedPart(WordPart):

  def __init__(self):
    WordPart.__init__(self, Id.Left_DoubleQuote)
    # TODO: Add token_type?  Id.Left_D_QUOTE, Id.Left_DD_QUOTE.  But what about
    # here doc?  It could be a dummy type.
    self.parts = []

  def __eq__(self, other):
    return self.parts == other.parts

  def __repr__(self):
    return '[DQ ' + ''.join(repr(p) for p in self.parts) + ']'

  def TokenPair(self):
    if self.parts:
      begin, _ = self.parts[0].TokenPair()
      _, end = self.parts[-1].TokenPair()
      return begin, end
    else:
      return None, None

  def EvalStatic(self):
    ret = ''
    for p in self.parts:
      ok, s, _ = p.EvalStatic()
      if not ok:
        return False, '', True
      ret += s

    return True, ret, True  # At least one part was quoted!


class CommandSubPart(WordPart):
  def __init__(self, token, command_list):
    WordPart.__init__(self, Id.Left_CommandSub)
    self.token = token
    self.command_list = command_list

  def __eq__(self, other):
    return self.command_list == other.command_list

  def __repr__(self):
    f = io.StringIO()
    self.command_list.PrintLine(f)  # print(on a single line)
    return '[ComSub %s]' % f.getvalue()

  def EvalStatic(self):
    return False, '', False

  def IsSubst(self):
    return True


class VarSubPart(WordPart):

  def __init__(self, name, token=None):
    """
    Args:
      name: a string, including '@' '*' '#' etc.?
      token: For debugging only?  Change to SourceLocation?
    """
    WordPart.__init__(self, Id.Left_VarSub)
    self.name = name
    self.token = token

    # This is the PARSED representation.  The executed representation will be
    # a tree of ExprNode.
    self.prefix_op = None  # e.g. VarOp0(VSubBang)
    self.bracket_op = None  # e.g. VarOp1(ExprNode) or VarOp0(Lit_At)
    self.suffix_op = None  # e.g. VarOp1(VTest) or VarOp1(VOp1) or VOp2

  def PrintLine(self, f):
    f.write('[VarSub %s' % self.name)  # no quotes around name
    if self.prefix_op:
      f.write(' prefix_op=%r' % self.prefix_op)
    if self.bracket_op:
      f.write(' bracket_op=%r' % self.bracket_op)
    if self.suffix_op:
      f.write(' suffix_op=%r' % self.suffix_op)
    f.write(']')

  def TokenPair(self):
    if self.token:
      return self.token, self.token
    else:
      return None, None

  def EvalStatic(self):
    return False, '', False

  def IsSubst(self):
    return True


class TildeSubPart(WordPart):
  # NOTE: NOT IsSubst

  def __init__(self, prefix):
    """
    Args:
      prefix: tilde prefix ("" if no prefix)
    """
    WordPart.__init__(self, Id.Lit_Tilde)
    self.prefix = prefix

  def __repr__(self):
    return '[TildeSub %r]' % self.prefix

  def EvalStatic(self):
    print('~ is not allowed')
    return False, '', False


class ArithSubPart(WordPart):

  def __init__(self, anode):
    # TODO: Do we want to also have Id.Left_ArithSub2 to preserve the source?
    # Although honestly, for most uses cases, it's probably fine to convert
    # everything to POSIX.
    WordPart.__init__(self, Id.Left_ArithSub)
    self.anode = anode

  def __repr__(self):
    return '[ArithSub %r]' % self.anode

  def EvalStatic(self):
    print('$(()) is not allowed)')
    return False, '', False

  def IsSubst(self):
    return True


class _BTokenInterface(object):
  """
  Common interface between unevaluated words (for [[ ) and evaluated words
  (for [ ).
  """
  def BoolId(self):
    """Return a token type for [[ and [."""
    raise NotImplementedError


class BToken(object):
  """Concrete class For [.

  Differences: uses -a and -o.  No ( ).

  Problem: In C++, you will have to use the parser at RUNTIME.  So it might
  have to be rewritten anyway.
  """
  def __init__(self, arg):
    self.arg = arg

  def BoolId(self):
    """Return a token type."""


#
# Word
#


class Word(_Node, _BTokenInterface):
  """A word or an operator."""

  def __init__(self):
    _Node.__init__(self, Id.Word_Compound)

  def __repr__(self):
    # repr() always prints as a single line
    f = io.StringIO()
    self.PrintLine(f)
    return f.getvalue()

  def PrintTree(self, f):
    raise NotImplementedError

  def PrintLine(self, f):
    raise NotImplementedError

  def TokenPair(self):
    """
    Returns:
      Leftmost token and rightmost token.
    """
    raise NotImplementedError

  # Interpret a word as an Id in three contexts.
  def ArithId(self):
    raise NotImplementedError

  def BoolId(self):
    raise NotImplementedError

  def CommandId(self):
    raise NotImplementedError

  def CommandKind(self):
    """Returns: Kind"""
    raise NotImplementedError


class CompoundWord(Word):
  """A word that is a sequence of WordPart instances"""

  def __init__(self, parts=None):
    Word.__init__(self)
    self.parts = parts or []  # public, mutable

  def __eq__(self, other):
    return self.parts == other.parts

  def PrintLine(self, f):
    # TODO: Consider indenting parts if they are complex, like a command sub
    # part.
    # NOTE: Is the K needed?  It's nice for human readability, but programs
    # might not need it.
    suffix = ' =' if self.LooksLikeAssignment() else ''
    s = '{' + ' '.join(repr(p) for p in self.parts) + ('%s}' % suffix)
    f.write(s)

  def TokenPair(self):
    if self.parts:
      begin_token, _ = self.parts[0].TokenPair()
      _, end_token = self.parts[-1].TokenPair()
      return begin_token, end_token
    else:
      return None, None

  # Interpret the words as 4 kinds of ID: Assignment, Arith, Bool, Command.
  # TODO: Might need other builtins.
  def AssignmentBuiltinId(self):
    """Tests if word is an assignment builtin."""
    # has to be a single literal part
    if len(self.parts) != 1:
      return Id.Undefined_Tok

    token_type = self.parts[0].LiteralId()
    if token_type == Id.Undefined_Tok:
      return Id.Undefined_Tok

    token_kind = LookupKind(token_type)
    if token_kind == Kind.Assign:
      return token_type

    return Id.Undefined_Tok

  def ArithId(self):
    return Id.Word_Compound

  def BoolId(self):
    if len(self.parts) != 1:
      return Id.Word_Compound

    token_type = self.parts[0].LiteralId()
    if token_type == Id.Undefined_Tok:
      return Id.Word_Compound

    # This is outside the BoolUnary/BoolBinary namespace, but works the same.
    if token_type in (Id.KW_Bang, Id.Lit_DRightBracket):
      return token_type

    token_kind = LookupKind(token_type)
    if token_kind in (Kind.BoolUnary, Kind.BoolBinary):
      return token_type

    return Id.Word_Compound

  def CommandId(self):
    # has to be a single literal part
    if len(self.parts) != 1:
      return Id.Word_Compound

    token_type = self.parts[0].LiteralId()
    if token_type == Id.Undefined_Tok:
      return Id.Word_Compound

    elif token_type in (Id.Lit_LBrace, Id.Lit_RBrace):
      return token_type

    token_kind = LookupKind(token_type)
    if token_kind == Kind.KW:
      return token_type

    return Id.Word_Compound

  def CommandKind(self):
    # NOTE: This is a bit inconsistent with CommandId, because we never retur
    # Kind.KW (or Kind.Lit).  But the CommandParser is easier to write this way.
    return Kind.Word

  def EvalStatic(self):
    """
    Returns a string, and whether any part was quoted.
    """
    ret = ''
    quoted = False
    for p in self.parts:
      ok, s, q = p.EvalStatic()
      if not ok:
        return False, '', quoted
      if q:
        quoted = True
      ret += s
    return True, ret, quoted

  def HasArrayPart(self):
    for part in self.parts:
      if part.id == Id.Right_ArrayLiteral:
        return True
    return False

  def LooksLikeAssignment(self):
    """
    Returns:
      String lhs, Word RHS
      or False

    TODO:  probably needs a different interface.
    """
    if len(self.parts) == 0:
      return False
    name = self.parts[0].VarLikeName()
    rhs = CompoundWord()
    if name:
      if len(self.parts) == 1:
        # NOTE: This is necesssary so that EmptyUnquoted elision isn't
        # applied.  EMPTY= is like EMPTY=''.
        rhs.parts.append(SingleQuotedPart())
      else:
        for p in self.parts[1:]:
          rhs.parts.append(p)
      return name, rhs
    return False

  def AsArithVarName(self):
    """Returns a string if this word looks like an arith var; otherwise False."""
    if len(self.parts) != 1:
      return ""

    return self.parts[0].ArithVarLikeName()  # may be empty

  def AsFuncName(self):
    ok, s, quoted = self.EvalStatic()
    if not ok:
      return False, ''
    if quoted:
      if len(self.parts) != 1:
        raise RuntimeError(
            "Function names should not have quotes, got: %s", self.parts)
    return True, s

  def BraceExpand(self):
    """
    Returns:
      A list of new Word instances, or None if there was no brace expansion
      detected.
    """
    # Algorithm:
    #
    # Look for patterns like LBRACE COMMA RBRACE
    # And then form cross product somehow.

    # "A correctly-formed brace expansion must contain unquoted opening and
    # closing braces, and at least one unquoted comma or a valid sequence
    # expression.  Any incorrectly formed brace expansion is left unchanged. "

    # Could this be recursive?  preamble,options,postscript
    #
    # Hm bash also has integer expressions!  {1..3} => {1,2,3}
    # {1..5..2} => {1,3,5}
    # - mksh doesn't have it.

    # look for subseqeuence like '{' ','+ '}'
    # And then make a data structure for this.


class TokenWord(Word):
  """A word that is just a token.

  NOTES:
  - The token range for this token may be more than one.  For example: a
    Id.Op_Newline is a token word that the CommandParser needs to know about.
    It may "own" Id.Ignored_Comment and Id.Ignored_Space nodes preceding it.
    These are tokens the CommandParser does NOT need to know about.
  - the Id.Eof_Real TokenWord owns and trailing whitespace.
  """
  def __init__(self, token):
    Word.__init__(self)
    self.token = token

  def __eq__(self, other):
    return self.token == other.token

  def PrintLine(self, f):
    f.write('{%s %s}' % (
        IdName(self.token.id), EncodeTokenVal(self.token.val)))

  def TokenPair(self):
    return self.token, self.token

  def ArithId(self):
    return self.token.id  # e.g. AS_PLUS

  def BoolId(self):
    return self.token.id

  def CommandId(self):
    return self.token.id

  def CommandKind(self):
    return self.token.Kind()
