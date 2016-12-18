#!/usr/bin/env python3
# Copyright 2016 Andy Chu. All rights reserved.
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#   http://www.apache.org/licenses/LICENSE-2.0
"""
bool_parse.py - Parse boolean expressions.

In contrast to test / [, the parsing of [[ expressions is done BEFORE
evaluation.  So we are parsing a list of Word instances to an AST, rather than
a list of Value instances.

TODO:
- Share parsing with "test / [".  That uses strings, while this uses words.

Grammar from http://compilers.iecc.com/crenshaw/tutor6.txt, adapted to ANTLR
syntax.

  Expr    : Term (OR Term)*
  Term    : Negated (AND Negated)*
  Negated : '!'? Factor
  Factor  : WORD
          | UNARY_OP WORD
          | WORD BINARY_OP WORD
          | '(' Expr ')'

OR = ||  -o
AND = &&  -a
WORD = any word
UNARY_OP: -z -n, etc.
BINARY_OP: -gt, -ot, ==, etc.

Other shell implementations:

bash has a recursive descent parser in parse.y:
parse_cond_command() / cond_expr() / ...
3 levels of precedence.

Bash manual:
https://www.gnu.org/software/bash/manual/bash.html#Conditional-Constructs

Precedence table.  Not sure why this is all one table, since [[ and (( are
separate: http://tldp.org/LDP/abs/html/opprecedence.html

mksh:
funcs.c: test_isop() / test_eval() /...
It is SHARED with [ using Test_env.

But mksh uses precedence climbing for the arithmetic parser.  Two different
algorithms!  See evalexpr() in expr.c.
"""

import sys

from osh import ast

from core import base
from core.id_kind import Id, Kind, LookupKind, IdName

from core.expr_node import UnaryExprNode, BinaryExprNode
from osh.lex import LexMode
try:
  from core import libc
except ImportError:
  from core import fake_libc as libc


class BoolParser(object):
  """Parses [[ at compile time and [ at runtime."""

  def __init__(self, w_parser):
    """
    Args:
      w_parser: WordParser
    """
    self.w_parser = w_parser
    # Either one word or two words for lookahead
    self.words = []

    self.cur_word = None
    self.op_id = Id.Undefined_Tok
    self.b_kind = Kind.Undefined

    self.error_stack = []

  def Error(self):
    return self.error_stack

  def AddErrorContext(self, msg, *args, token=None, word=None):
    err = base.MakeError(msg, *args, token=token, word=word)
    self.error_stack.append(err)

  def _NextOne(self, lex_mode=LexMode.DBRACKET):
    #print('_Next', self.cur_word)
    n = len(self.words)
    if n == 2:
      assert lex_mode == LexMode.DBRACKET
      self.words[0] = self.words[1]
      self.cur_word = self.words[0]
      del self.words[1]
    elif n in (0, 1):
      w = self.w_parser.ReadWord(lex_mode)
      if not w:
        err = self.w_parser.Error()
        self.error_stack.extend(err)
        return False
      if n == 0:
        self.words.append(w)
      else:
        self.words[0] = w
      self.cur_word = w

    self.op_id = self.cur_word.BoolId()
    self.b_kind = LookupKind(self.op_id)
    #print('---- word', self.cur_word, 'op_id', self.op_id, self.b_kind, lex_mode)
    return True

  def _Next(self, lex_mode=LexMode.DBRACKET):
    """Advance to the next token, skipping newlines.

    We don't handle newlines in the lexer because we want the newline after ]]
    to be Id.Op_Newline rather than Id.WS_Newline.  It's more complicated if
    it's Id.WS_Newline -- we might have to unread tokens, etc.
    """
    while True:
      w = self._NextOne(lex_mode=lex_mode)
      if not w:
        return False
      if self.op_id != Id.Op_Newline:
        break
    return True

  def AtEnd(self):
    #print('B_ID', IdName(self.op_id), self.cur_word)
    return self.op_id == Id.Lit_DRightBracket

  def _LookAhead(self):
    n = len(self.words)
    if n != 1:
      raise AssertionError(self.words)

    w = self.w_parser.ReadWord(LexMode.DBRACKET)
    self.words.append(w)  # Save it for _Next()
    return w

  def Parse(self):
    if not self._Next(): return None

    node = self.ParseExpr()
    if not self.AtEnd():
      self.AddErrorContext("Unexpected extra word %r", self.cur_word,
          word=self.cur_word)
      return None
    return node

  def ParseExpr(self):
    """
    Iterative:
    Expr    : Term (OR Term)*

    Right recursion:
    Expr    : Term (OR Expr)?
    """
    left = self.ParseTerm()
    if self.op_id == Id.Op_DPipe:
      if not self._Next(): return None
      right = self.ParseExpr()
      return BinaryExprNode(Id.Op_DPipe, left, right)
    else:
      return left

  def ParseTerm(self):
    """
    Term    : Negated (AND Negated)*

    Right recursion:
    Term    : Negated (AND Term)?
    """
    left = self.ParseNegatedFactor()
    if self.op_id == Id.Op_DAmp:
      if not self._Next(): return None
      right = self.ParseTerm()
      return BinaryExprNode(Id.Op_DAmp, left, right)
    else:
      return left

  def ParseNegatedFactor(self):
    """
    Negated : '!'? Factor
    """
    if self.op_id == Id.KW_Bang:
      if not self._Next(): return None
      child = self.ParseFactor()
      #return UnaryExprNode(Id.KW_Bang, child)
      return ast.LogicalNot(child)
    else:
      return self.ParseFactor()

  def ParseFactor(self):
    """
    Factor  : WORD
            | UNARY_OP WORD
            | WORD BINARY_OP WORD
            | '(' Expr ')'
    """
    #print('ParseFactor %s %s' % (self.b_kind, IdName(self.op_id)))
    if self.b_kind == Kind.BoolUnary:
      # Just save the type and not the token itself?
      op = self.op_id
      if not self._Next(): return None
      word = self.cur_word
      if not self._Next(): return None
      node = UnaryExprNode(op, word)
      return node

    if self.b_kind == Kind.Word:
      # Peek ahead another token.
      t2 = self._LookAhead()
      t2_op_id = t2.BoolId()
      t2_b_kind = LookupKind(t2_op_id)

      # Redir PUN for < and >
      if t2_b_kind in (Kind.BoolBinary, Kind.Redir):
        left = self.cur_word

        if not self._Next(): return None
        op = self.op_id

        # TODO: Need to change to LexMode.BASH_REGEX.
        # _Next(lex_mode) then?
        is_regex = t2_op_id == Id.BoolBinary_EqualTilde
        if is_regex:
          if not self._Next(lex_mode=LexMode.BASH_REGEX): return None
        else:
          if not self._Next(): return None

        right = self.cur_word
        if is_regex:
          ok, regex_str, unused_quoted = right.EvalStatic()
          # doesn't contain $foo, etc.
          if ok and not libc.regex_parse(regex_str):
            self.AddErrorContext("Invalid regex: %r" % regex_str, word=right)
            return None

        if not self._Next(): return None
        return BinaryExprNode(op, left, right)
      else:
        # [[ foo ]] is implicit Implicit [[ -n foo ]]
        #op = Id.BoolUnary_n
        word = self.cur_word
        if not self._Next(): return None
        #return UnaryExprNode(op, word)
        return word

    if self.op_id == Id.Op_LParen:
      if not self._Next(): return None
      node = self.ParseExpr()
      if self.op_id != Id.Op_RParen:
        raise RuntimeError("Expected ), got %s", self.cur_word)
      if not self._Next(): return None
      return node

    # TODO: A proper error, e.g. for "&&"
    raise AssertionError("Unexpected token: %s" % self.cur_word)


def main(argv):
  import expr_eval
  import bool_parse_test
  import cmd_exec
  import word_eval

  p = bool_parse_test._MakeParser(argv[1])
  node = p.ParseExpr()
  assert p.AtEnd()
  print('node:', node)

  mem = cmd_exec.Mem('', [])
  exec_opts = cmd_exec.ExecOpts()
  ev = word_eval.CompletionEvaluator(mem, exec_opts)

  ok, b = expr_eval.BEval(node, ev)
  print('result:', ok, b)


if __name__ == '__main__':
  try:
    main(sys.argv)
  except NotImplementedError:
    raise
  except RuntimeError as e:
    print('FATAL: %r' % e, file=sys.stderr)
    sys.exit(1)
