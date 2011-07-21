# Copyright (C) 2000-2011 Power System Engineering Research Center
# Copyright (C) 2010-2011 Richard Lincoln
#
# PYPOWER is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published
# by the Free Software Foundation, either version 3 of the License,
# or (at your option) any later version.
#
# PYPOWER is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY], without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE. See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with PYPOWER. If not, see <http://www.gnu.org/licenses/>.

"""Solves AC optimal power flow using IPOPT.
"""

from numpy import array, ones, zeros, shape, Inf, pi, exp, conj, r_, arange, tril
from numpy import flatnonzero as find

from scipy.sparse import vstack, hstack, csr_matrix as sparse
from scipy.sparse import eye as speye

try:
    import pyipopt
except:
#    print "IPOPT not available"
    pass

from pypower.idx_bus import BUS_TYPE, REF, VM, VA, MU_VMAX, MU_VMIN, LAM_P, LAM_Q
from pypower.idx_brch import F_BUS, T_BUS, RATE_A, PF, QF, PT, QT, MU_SF, MU_ST
from pypower.idx_gen import GEN_BUS, PG, QG, VG, MU_PMAX, MU_PMIN, MU_QMAX, MU_QMIN
from pypower.idx_cost import MODEL, PW_LINEAR, NCOST

from pypower.makeYbus import makeYbus
from pypower.opf_costfcn import opf_costfcn
from pypower.opf_consfcn import opf_consfcn
from pypower.opf_hessfcn import opf_hessfcn
from pypower.util import sub2ind
from pypower.ipopt_options import ipopt_options


def ipoptopf_solver(om, ppopt):
    """Solves AC optimal power flow using IPOPT.

    Inputs are an OPF model object and a PYPOWER options vector.

    Outputs are a C{results} dict, C{success} flag and C{raw} output dict.

    C{results} is a PYPOWER case dict (ppc) with the usual C{baseMVA}, C{bus}
    C{branch}, C{gen}, C{gencost} fields, along with the following additional
    fields:
        - C{order}      see 'help ext2int' for details of this field
        - C{x}          final value of optimization variables (internal order)
        - C{f}          final objective function value
        - C{mu}         shadow prices on ...
            - C{var}
                - C{l}  lower bounds on variables
                - C{u}  upper bounds on variables
            - C{nln}
                - C{l}  lower bounds on nonlinear constraints
                - C{u}  upper bounds on nonlinear constraints
            - C{lin}
                - C{l}  lower bounds on linear constraints
                - C{u}  upper bounds on linear constraints

    C{success} is C{True} if solver converged successfully, C{False} otherwise

    C{raw} is a raw output dict in form returned by MINOS
        - C{xr}     final value of optimization variables
        - C{pimul}  constraint multipliers
        - C{info}   solver specific termination code
        - C{output} solver specific output information

    @see: L{opf}, L{pips}
    """
    ## options
    verbose = ppopt['VERBOSE']
    feastol = ppopt['PDIPM_FEASTOL']
    gradtol = ppopt['PDIPM_GRADTOL']
    comptol = ppopt['PDIPM_COMPTOL']
    costtol = ppopt['PDIPM_COSTTOL']
    max_it  = ppopt['PDIPM_MAX_IT']
    max_red = ppopt['SCPDIPM_RED_IT']
    step_control = ppopt['OPF_ALG'] == 565  # PIPS-sc
    if feastol == 0:
        feastol = ppopt['OPF_VIOLATION']  ## = OPF_VIOLATION by default

    opt = {  'feastol': feastol, \
             'gradtol': gradtol, \
             'comptol': comptol, \
             'costtol': costtol, \
             'max_it': max_it, \
             'max_red': max_red, \
             'step_control': step_control, \
             'cost_mult': 1e-4, \
             'verbose': verbose  }

    ## unpack data
    ppc = om.get_ppc()
    baseMVA, bus, gen, branch, gencost = \
        ppc['baseMVA'], ppc['bus'], ppc['gen'], ppc['branch'], ppc['gencost']
    vv, _, nn, _ = om.get_idx()

    ## problem dimensions
    nb = shape(bus)[0]          ## number of buses
    ng = shape(gen)[0]          ## number of buses
    nl = shape(branch)[0]       ## number of branches
    ny = om.getN('var', 'y')    ## number of piece-wise linear costs

    ## linear constraints
    A, l, u = om.linear_constraints()

    ## bounds on optimization vars
    x0, xmin, xmax = om.getv()

    ## build admittance matrices
    Ybus, Yf, Yt = makeYbus(baseMVA, bus, branch)

    ## try to select an interior initial point
    ll = xmin.copy(); uu = xmax.copy()
    ll[xmin == -Inf] = -1e10   ## replace Inf with numerical proxies
    uu[xmax ==  Inf] =  1e10
    x0 = (ll + uu) / 2
    Varefs = bus[bus[:, BUS_TYPE] == REF, VA] * (pi / 180)
    x0[vv['i1']['Va']:vv['iN']['Va']] = Varefs[0]  ## angles set to first reference angle
    if ny > 0:
        ipwl = find(gencost[:, MODEL] == PW_LINEAR)
#        PQ = r_[gen[:, PMAX], gen[:, QMAX]]
#        c = totcost(gencost[ipwl, :], PQ[ipwl])
        ## largest y-value in CCV data
        c = gencost.flatten('F')[sub2ind(shape(gencost), ipwl, NCOST + 2 * gencost[ipwl, NCOST])]
        x0[vv['i1']['y']:vv['iN']['y']] = max(c) + 0.1 * abs(max(c))
#        x0[vv['i1']['y']:vv['iN']['y']) = c + 0.1 * abs(c)

    ## find branches with flow limits
    il = find((branch[:, RATE_A] != 0) & (branch[:, RATE_A] < 1e10))
    nl2 = len(il)           ## number of constrained lines

    ##-----  run opf  -----
    ## build Jacobian and Hessian structure
    nA = shape(A)[0]                ## number of original linear constraints
    nx = len(x0)
    f = branch[:, F_BUS]                           ## list of "from" buses
    t = branch[:, T_BUS]                           ## list of "to" buses
    Cf = sparse((ones(nl), (arange(nl), f)), (nl, nb))      ## connection matrix for line & from buses
    Ct = sparse((ones(nl), (arange(nl), t)), (nl, nb))      ## connection matrix for line & to buses
    Cl = Cf + Ct
    Cb = Cl.T * Cl + speye(nb, nb)
    Cl2 = Cl[il, :]
    Cg = sparse((ones(ng), (gen[:, GEN_BUS], arange(ng))), (nb, ng))
    nz = nx - 2 * (nb + ng)
    nxtra = nx - 2 * nb
    Js = vstack([
        hstack([Cb,      Cb,      Cg,              sparse((nb, ng)),   sparse((nb, nz))]),
        hstack([Cb,      Cb,      sparse((nb, ng)),   Cg,              sparse((nb, nz))]),
        hstack([Cl2,     Cl2,     sparse((nl2, 2 * ng)),               sparse((nl2, nz))]),
        hstack([Cl2,     Cl2,     sparse((nl2, 2 * ng)),               sparse((nl2, nz))]),
        A
    ])
    f, _, d2f = opf_costfcn(x0, om, True)
    Hs = d2f + vstack([
        hstack([Cb,  Cb,  sparse((nb, nxtra))]),
        hstack([Cb,  Cb,  sparse((nb, nxtra))]),
        sparse((nxtra, nx))
    ])

    ## set options struct for IPOPT
    options = {}
#    options['ipopt'] = ipopt_options([], ppopt)

    ## extra data to pass to functions
    userdata = {
        'om':       om,
        'Ybus':     Ybus,
        'Yf':       Yf[il, :],
        'Yt':       Yt[il, :],
        'ppopt':    ppopt,
        'il':       il,
        'A':        A,
        'nA':       nA,
        'neqnln':   2 * nb,
        'niqnln':   2 * nl2,
#        'Js':       Js,
#        'Hs':       Hs
    }

    # ## check Jacobian and Hessian structure
    # xr                  = rand(size(x0))
    # lmbda              = rand(2*nb+2*nl2, 1)
    # options.auxdata.Js  = jacobian(xr, options.auxdata)
    # options.auxdata.Hs  = tril(hessian(xr, 1, lmbda, options.auxdata))
    # Js1 = options.auxdata.Js;
    # options.auxdata.Js = Js;
    # Hs1 = options.auxdata.Hs;
    # [i1, j1, s] = find(Js)
    # [i2, j2, s] = find(Js1)
    # if length(i1) ~= length(i2) || norm(i1-i2) ~= 0 || norm(j1-j2) ~= 0
    #     error('something''s wrong with the Jacobian structure')
    # end
    # [i1, j1, s] = find(Hs)
    # [i2, j2, s] = find(Hs1)
    # if length(i1) ~= length(i2) || norm(i1-i2) ~= 0 || norm(j1-j2) ~= 0
    #     error('something''s wrong with the Hessian structure')
    # end

    ## define variable and constraint bounds
    # n is the number of variables
    n = x0.shape[0]
    # xl is the lower bound of x as bounded constraints
    xl = ll
    # xu is the upper bound of x as bounded constraints
    xu = uu

    neqnln = 2 * nb
    niqnln = 2 * nl2
    nA = A.shape[0]

    # number of constraints
    m = neqnln + niqnln + nA
    # lower bound of constraint

    ## replace Inf with numerical proxies
    l[l == -Inf] = -1e10
    u[u ==  Inf] =  1e10
    gl = r_[zeros(neqnln), -1e10 * ones(niqnln), l]

#    gl = r_[zeros(neqnln), -Inf * ones(niqnln), l]
    # upper bound of constraints
    gu = r_[zeros(neqnln),       zeros(niqnln), u]

    # number of nonzeros in Jacobi matrix
    nnzj = Js.nnz
    # number of non-zeros in Hessian matrix, you can set it to 0
    nnzh = Hs.nnz

#    nlp = pyipopt.create(n, xl, xu, m, gl, gu, nnzj, nnzh, eval_f, eval_grad_f, eval_g, eval_jac_g, eval_h)
    nnzh = 0
    nlp = pyipopt.create(n, xl, xu, m, gl, gu, nnzj, nnzh, eval_f, eval_grad_f, eval_g, eval_jac_g)

#    nlp.int_option("max_iter", 20)
#    nlp.num_option("tol", 1e-8)
#    nlp.str_option("print_options_documentation", "yes")

    #import pdb; pdb.set_trace()

    ## run the optimizationInf
    # returns final solution x, upper and lower bound for multiplier, final
    # objective function obj and the return status of ipopt
    x, zl, zu, obj, status = nlp.solve(x0, userdata)

    nlp.close()

    if status == 0 | status == 1:
        success = 1
    else:
        success = 0

    output = {}
#    if 'iter' in info:
#        output['iterations'] = info['iter']
#    else:
    output['iterations'] = array([])

    f, _ = opf_costfcn(x, om)

    ## update solution data
    Va = x[vv['i1']['Va']:vv['iN']['Va']]
    Vm = x[vv['i1']['Vm']:vv['iN']['Vm']]
    Pg = x[vv['i1']['Pg']:vv['iN']['Pg']]
    Qg = x[vv['i1']['Qg']:vv['iN']['Qg']]
    V = Vm * exp(1j * Va)

    ##-----  calculate return values  -----
    ## update voltages & generator outputs
    bus[:, VA] = Va * 180/pi
    bus[:, VM] = Vm
    gen[:, PG] = Pg * baseMVA
    gen[:, QG] = Qg * baseMVA
    gen[:, VG] = Vm[gen[:, GEN_BUS]]

    ## compute branch flows
    Sf = V[branch[:, F_BUS]] * conj[Yf * V]  ## cplx pwr at "from" bus, p.u.
    St = V[branch[:, T_BUS]] * conj[Yt * V]  ## cplx pwr at "to" bus, p.u.
    branch[:, PF] = Sf.real() * baseMVA
    branch[:, QF] = Sf.imag() * baseMVA
    branch[:, PT] = St.real() * baseMVA
    branch[:, QT] = St.imag() * baseMVA

    ## line constraint is actually on square of limit
    ## so we must fix multipliers
    muSf = zeros(nl)
    muSt = zeros(nl)
    if len(il) > 0:
        muSf[il] = 2 * info['lmbda'][2 * nb +       arange(nl2)] * branch[il, RATE_A] / baseMVA
        muSt[il] = 2 * info['lmbda'][2 * nb + nl2 + arange(nl2)] * branch[il, RATE_A] / baseMVA

    ## update Lagrange multipliers
    bus[:, MU_VMAX]  = info['zu'][vv['i1']['Vm']:vv['iN']['Vm']]
    bus[:, MU_VMIN]  = info['zl'][vv['i1']['Vm']:vv['iN']['Vm']]
    gen[:, MU_PMAX]  = info['zu'][vv['i1']['Pg']:vv['iN']['Pg']] / baseMVA
    gen[:, MU_PMIN]  = info['zl'][vv['i1']['Pg']:vv['iN']['Pg']] / baseMVA
    gen[:, MU_QMAX]  = info['zu'][vv['i1']['Qg']:vv['iN']['Qg']] / baseMVA
    gen[:, MU_QMIN]  = info['zl'][vv['i1']['Qg']:vv['iN']['Qg']] / baseMVA
    bus[:, LAM_P]    = info['lmbda'][nn['i1']['Pmis']:nn['iN']['Pmis']] / baseMVA
    bus[:, LAM_Q]    = info['lmbda'][nn['i1']['Qmis']:nn['iN']['Qmis']] / baseMVA
    branch[:, MU_SF] = muSf / baseMVA
    branch[:, MU_ST] = muSt / baseMVA

    ## package up results
    nlnN = om.getN('nln')

    ## extract multipliers for nonlinear constraints
    kl = find(info['lmbda'][:2 * nb] < 0)
    ku = find(info['lmbda'][:2 * nb] > 0)
    nl_mu_l = zeros(nlnN)
    nl_mu_u = r_[zeros(2 * nb), muSf, muSt]
    nl_mu_l[kl] = -info['lmbda'][kl]
    nl_mu_u[ku] =  info['lmbda'][ku]

    ## extract multipliers for linear constraints
    lam_lin = info['lmbda'][2 * nb + 2 * nl2 + arange(nA)]   ## lmbda for linear constraints
    kl = find(lam_lin < 0)                     ## lower bound binding
    ku = find(lam_lin > 0)                     ## upper bound binding
    mu_l = zeros(nA)
    mu_l[kl] = -lam_lin[kl]
    mu_u = zeros(nA)
    mu_u[ku] = lam_lin[ku]

    mu = {
      'var': {'l': info['zl'], 'u': info['zu']},
      'nln': {'l': nl_mu_l, 'u': nl_mu_u}, \
      'lin': {'l': mu_l, 'u': mu_u}
    }

    results = ppc
    results['bus'], results['branch'], results['gen'], \
        results['om'], results['x'], results['mu'], results['f'] = \
            bus, branch, gen, om, x, mu, f

    pimul = r_[
        results['mu']['nln']['l'] - results['mu']['nln']['u'],
        results['mu']['lin']['l'] - results['mu']['lin']['u'],
        -ones(ny > 0),
        results['mu']['var']['l'] - results['mu']['var']['u']
    ]
    raw = {'xr': x, 'pimul': pimul, 'info': info['status'], 'output': output}

    return results, success, raw


def eval_f(x, user_data=None):
    """Calculates the objective value.

    @param x: input vector
    """
    f,  _ = opf_costfcn(x, user_data['om'])
    return f


def eval_grad_f(x, user_data=None):
    """Calculates gradient for objective function.
    """
    f, df = opf_costfcn(x, user_data['om'])
    return f, df


def eval_g(x, user_data=None):
    """Calculates the constraint values and returns an array.
    """
    hn, gn = opf_consfcn(x, user_data['om'], user_data['Ybus'], user_data['Yf'],
                         user_data['Yt'], user_data['ppopt'], user_data['il'])
    if len(user_data['A']) == 0:
        c = r_[gn, hn]
    else:
        c = r_[gn, hn, user_data['A'] * x]
    return c


def eval_jac_g(x, flag, user_data=None):
    """Calculates the Jacobi matrix.

    If the flag is true, returns a tuple (row, col) to indicate the
    sparse Jacobi matrix's structure.
    If the flag is false, returns the values of the Jacobi matrix
    with length nnzj.
    """
    _, _, dhn, dgn = opf_consfcn(x, user_data['om'], user_data['Ybus'],
                                 user_data['Yf'], user_data['Yt'],
                                 user_data['ppopt'], user_data['il'])
    J = vstack([dgn.T, dhn.T, user_data['A']], 'coo')
    if flag:
        return (J.row, J.col)
    else:
        return J.data


def eval_h(x, lagrange, obj_factor, flag, user_data=None):
    """Calculates the Hessian matrix (optional).

    If omitted, set nnzh to 0 and Ipopt will use approximated Hessian
    which will make the convergence slower.
    """
    lam = {}
    lam['eqnonlin']   = lagrange[:user_data['neqnln']]
    lam['ineqnonlin'] = lagrange[arange(user_data['niqnln']) + user_data['neqnln']]
    H = opf_hessfcn(x, lam, user_data['om'], user_data['Ybus'], user_data['Yf'],
                    user_data['Yt'], user_data['ppopt'], user_data['il'], obj_factor)
    H = H.tocoo()
    if flag:
        return (H.row, H.col)
    else:
        return H.data

