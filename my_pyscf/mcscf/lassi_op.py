import numpy as np
from pyscf import lib
from pyscf.fci.direct_spin1 import _unpack_nelec
from pyscf.fci.addons import cre_a, cre_b, des_a, des_b
from itertools import product, combinations
import time

def lst_hopping_index (fciboxes, nlas, nelelas, idx_root):
    ''' Build the LAS state transition hopping index

        Args:
            fciboxes: list of h1e_zipped_fcisolvers
            nlas: list of norbs for each fragment
            nelelas: list of neleca + nelecb for each fragment
            idx_root: integer indices of roots in this symmetry block

        Returns:
            hopping_index: ndarray of ints of shape (nfrags, 2, nroots, nroots)
                element [i,j,k,l] reports the change of number of electrons of
                spin l in fragment i between LAS states j and k
            zerop_index: ndarray of bools of shape (nroots, nroots)
                element [i,j] is true where the ith and jth LAS states are
                connected by a null excitation; i.e., no electron, pair,
                or spin hopping or pair splitting/coalescence. This implies
                nonzero 1- and 2-body transition density matrices within
                all fragments.
            onep_index: ndarray of bools of shape (nroots, nroots)
                element [i,j] is true where the ith and jth LAS states
                are connected by exactly one electron hop from i to j or vice
                versa, implying nonzero 1-body transition density matrices
                within spectator fragments and phh/pph modes within
                source/dest fragments.
    '''
    nelelas = [sum (_unpack_nelec (ne)) for ne in nelelas]
    nelec_fsr = np.array ([[_unpack_nelec (fcibox._get_nelec (fcibox.fcisolvers[ix], ne))
        for ix in idx_root] for fcibox, ne in zip (fciboxes, nelelas)]).transpose (0,2,1)
    hopping_index = np.array ([[np.subtract.outer (spin, spin)
        for spin in frag] for frag in nelec_fsr])
    symm_index = np.all (hopping_index.sum (0) == 0, axis=0)
    zerop_index = symm_index & (np.count_nonzero (hopping_index, axis=(0,1)) == 0)
    onep_index = symm_index & (np.abs (hopping_index).sum ((0,1)) == 2)
    return hopping_index, zerop_index, onep_index

class LSTDMint (object):
    ''' Sparse-memory storage for LAS-state transition density matrix 
        single-fragment intermediates. '''

    def __init__(self, fcibox, norb, nelec, nroots, idx_root):
        # I'm not sure I need linkstrl
        self.fcisolvers = [fcibox.fcisolvers[ix] for ix in idx_root]
        self.linkstrl = fcibox.states_gen_linkstr (norb, nelec, tril=True)
        self.linkstr = fcibox.states_gen_linkstr (norb, nelec, tril=False)
        self.norb = norb
        self.nelec = nelec
        self.nroots = nroots
        self.nelec_r = [fcibox._get_nelec (solver, nelec) for solver in self.fcisolvers]
        self._h = [[[None for i in nroots] for j in nroots] for s in (0,1)]
        self._hh = [[[None for i in nroots] for j in nroots] for s in (-1,0,1)] 
        self._phh = [[[None for i in nroots] for j in nroots] for s in (0,1)]
        self._sm = [[None for i in nroots] for j in nroots]
        self.dm1 = [[None for i in nroots] for j in nroots]
        self.dm2 = [[None for i in nroots] for j in nroots]

    # 1-particle 1-operator intermediate

    def get_h (self, i, j, s):
        return self._h[s][i][j]

    def set_h (self, i, j, s, x):
        self._h[s][i][j] = x
        return x

    def get_p (self, i, j, s):
        return self._h[s][j][i].conj ()

    # 2-particle intermediate

    def get_hh (self, i, j, s):
        return self._hh[s][i][j]

    def set_hh (self, i, j, s, x):
        self._hh[s][i][j] = x
        return x

    def get_pp (self, i, j, s):
        return self._hh[s][j][i].conj ().T

    # 1-particle 3-operator intermediate

    def get_phh (self, i, j, s):
        return self._phh[s][i][j]

    def set_phh (self, i, j, s, x):
        self._phh[s][i][j] = x
        return x

    def get_pph (self, i, j, s):
        return self._phh[s][j][i].conj ().transpose (0,2,1)

    # spin-hop intermediate

    def get_sm (self, i, j):
        return self._sm[i][j]

    def set_sm (self, i, j, x):
        self._sm[i][j] = x
        return x

    def get_sp (self, i, j):
        return self._sm[j][i].conj ()

    # 1-density intermediate

    def get_dm1 (self, i, j):
        k, l = max (i, j), min (i, j)
        return self.dm1[k][l]

    def set_dm1 (self, i, j, x):
        if j > i:
            self.dm1[j][i] = x.conj ().transpose (0, 2, 1)
        else:
            self.dm1[i][j] = x

    # 2-density intermediate

    def get_dm2 (self, i, j):
        k, l = max (i, j), min (i, j)
        return self.dm2[k][l]

    def set_dm2 (self, i, j, x):
        if j > i:
            self.dm2[j][i] = x.conj ().transpose (0, 2, 1, 4, 3)
        else:
            self.dm2[i][j] = x

    def kernel (self, ci, hopping_index, zerop_index, onep_index):
        nroots, norb = self.nroots, self.norb
        t0 = (time.clock (), time.time ())

        # Spectator fragment contribution
        spectator_index = np.all (hopping_index == 0, axis=0)
        spectator_index[np.triu_indices (self.nroots, k=1)] = False
        spectator_index = np.stack (np.where (spectator_index), axis=0)
        for i, j in spectator_index:
            solver = self.fcisolvers[i]
            linkstr = self.linkstr[i]
            nelec = self.nelec_r[i]
            dm1s, dm2s = solver.trans_rdm12s (ci[i], ci[j], norb, nelec, link_index=linkstr) 
            self.set_dm1 (i, j, dm1s)
            if zerop_index[i,j]: self.set_dm2 (i, j, dm2s)

        # Cache some b_p|i> beforehand for the sake of the spin-flip intermediate 
        hidx_ket_a = np.where (np.any (hopping_index[0] < 0, axis=0))[0]
        hidx_ket_b = np.where (np.any (hopping_index[1] < 0, axis=0))[0]
        bpvec_list = [None for ket in range (nroots)]
        for ket in hidx_ket_b:
            if np.any (np.all (hopping_index[:,:,ket] == [1,-1]), axis=1):
                bpvec_list[ket] = np.stack ([des_b (self.ci[ket], norb, self.nelec_r[ket], p) for p in range (norb)], axis=0)

        # a_p|i>
        for ket in hidx_ket_a:
            nelec = self.nelec_r[ket]
            apket = np.stack ([des_a (self.ci[ket], norb, nelec, p) for p in range (norb)], axis=0)
            nelec = (nelec[0]-1, nelec[1])
            # <j|a_p|i>
            for bra in np.where (hopping_index[0,:,ket] < 0)[0]:
                bravec = self.ci[bra].ravel ()
                self.set_h (bra, ket, 0, bravec.dot (apket.reshape (norb,-1).T))
                # <j|a'_q a_r a_p|i>, <j|b'_q b_r a_p|i>
                if np.all (hopping_index[:,bra,ket] == [-1,0]) and onep_index[bra,ket]:
                    solver = self.fcisolvers[bra]
                    linkstr = self.linkstr[bra]
                    self.set_phh (bra, ket, 0, np.stack ([solver.trans_rdm12s
                        (self.ci[bra], ketmat, norb, self.nelec_r[bra], link_index=linkstr)[0]
                        for ketmat in apket], axis=-1))
                # <j|b'_q a_p|i> = <j|s-|i>
                elif np.all (hopping_index[:,bra,ket] == [-1,1]):
                    aqbra = bpvec_list[bra].reshape (norb, -1).conj ()
                    self.set_sm (bra, ket, np.dot (aqbra, apket.reshape (norb, -1).T))
                # <j|b_q a_p|i>
                elif np.all (hopping_index[:,bra,ket] == [-1,-1]):
                    hh = np.array ([[np.dot (bravec, des_b (pket, norb, nelec, q).ravel ())
                        for pket in apket] for q in range (norb)])
                    self.set_hh (bra, ket, 1, hh)
                # <j|a_q a_p|i>
                elif np.all (hopping_index[:,bra,ket] == [-2,0]):
                    hh_triu = [bravec.dot (des_a (apket[p], norb, nelec, q).ravel ())
                        for q, p in combinations (range (norb), 2)]
                    hh = np.zeros ((norb, norb), dtype = apket.dtype)
                    hh[np.triu_indices (norb, k=1)] = hh_triu
                    hh -= hh.T
                    self.set_hh (bra, ket, 0, hh)                
                
        # b_p|i>
        for ket in hidx_ket_b:
            nelec = self.nelec_r[ket]
            bpvec = np.stack ([des_b (self.ci[ket], norb, nelec, p)
                for p in range (norb)], axis=0) if bpvec_list[ket] is None else bpvec_list[ket]
            nelec = (nelec[0], nelec[1]-1)
            # <j|b_p|i>
            for bra in np.where (hopping_index[1,:,ket] < 0)[0]:
                bravec = self.ci[bra].ravel ()
                self.set_h (bra, ket, 1, bravec.dot (bpvec.reshape (norb,-1).T))
                # <j|a'_q a_r b_p|i>, <j|b'_q b_r b_p|i>
                if np.all (hopping_index[:,bra,ket] == [0,-1]) and onep_index[bra,ket]:
                    solver = self.fcisolvers[bra]
                    linkstr = self.linkstr[bra]
                    self.set_phh (bra, ket, 1, np.stack ([solver.trans_rdm12s
                        (self.ci[bra], ketmat, norb, self.nelec_r[bra], link_index=linkstr)[0]
                        for ketmat in bpvec], axis=-1))
                # <j|b_q b_p|i>
                elif np.all (hopping_index[:,bra,ket] == [0,-2]):
                    hh_triu = [bravec.dot (des_b (bpvec[p], norb, nelec, q).ravel ())
                        for q, p in combinations (range (norb), 2)]
                    hh = np.zeros ((norb, norb), dtype = bpvec.dtype)
                    hh[np.triu_indices (norb, k=1)] = hh_triu
                    hh -= hh.T
                    self.set_hh (bra, ket, 2, hh)                
        
        return t0

def make_stdm12s (las, ci, _0, _1, idx_root, **kwargs):
    fciboxes = las.fciboxes
    nlas = las.ncas_sub
    nelelas = [sum (_unpack_nelec (ne)) for ne in las.nelecas_sub]
    ncas = las.ncas
    nfrags = len (fciboxes)
    nroots = np.count_nonzero (idx_root)
    idx_root = np.where (idx_root)[0]
    nelelas_rs = [(sum (nefrag[i][0] for nefrag in nelelas_frs), sum (nefrag[i][1] for nefrag in nelelas_frs)) for i in range (nroots)]
    tdm1s = np.zeros ((nroots, nroots, 2, ncas, ncas), dtype=ci[0][0].dtype) 
    tdm2s = np.zeros ((nroots, nroots, 4, ncas, ncas, ncas, ncas), dtype=ci[0][0].dtype) 

    # First pass: single-fragment intermediates
    hopping_index, zerop_index, onep_index = lst_hopping_index (fciboxes, nlas, nelelas, idx_root)
    ints = []
    for ifrag in range (nfrags);
        tdmint = LSTDMint (fciboxes[ifrag], nlas[ifrag], nelelas[ifrag], nroots, idx_root)
        t0 = tdmint.kernel (ci[ifrag], hopping_index[ifrag], zerop_index, onep_index)
        lib.logger.timer (las, 'LAS-state TDM12s intermediate crunching', *t0)        
        ints.append (tdmint)

    # Process connectivity data to quickly distinguish interactions
    conserv_index = np.all (hopping_index.sum (1) == 0, axis=0)
    nsop_index = np.abs (hopping_index).sum (0) # 0,0 , 2,0 , 0,2 , 2,2 , 4,0 , 0,4
    nop_index = nsop_index.sum (0) # 0, 2, 4
    nfrag_index = np.count_nonzero (np.abs (hopping_index).sum (1), axis=0) # 0-4
    ncharge_index = np.count_nonzero (hopping_index).sum (1), axis=0 # = 0 for spin modes
    nspin_index = nsop_index[1,:,:] // 2 
    # This last ^ is somewhat magical, but notice that it corresponds to the mapping
    #   2,0 ; 4,0 -> 0 -> a or aa
    #   0,2 ; 2,2 -> 1 -> b or ab
    #   0,4       -> 2 -> bb
    # Provided one only looks at symmetry-allowed interactions of order 1 or 2

    # Cruncher functions
    def _crunch_null (bra, ket):
        d1 = tdm1s[bra,ket]
        d2 = tdm2s[bra,ket]
        for i, inti in enumerate (ints):
            p = sum (nlas[:i])
            q = p + nlas[i]
            d1_s_ii = inti.get_dm1 (bra, ket)
            d1[:,p:q,p:q] = d1_s_ii
            d2[:,p:q,p:q,p:q,p:q] = inti.get_dm2 (bra, ket)
            for j, intj in enumerate (ints[:i]):
                assert (i>j)
                r = sum (nlas[:j])
                s = r + nlas[j]
                d1_s_jj = intj.get_dm1 (bra, ket)
                d2_s_iijj = np.multiply.outer (d1_s_ii, d1_s_jj).transpose (0,3,1,2,4,5)
                d2_s_iijj = d2_s_iijj.reshape (4, q-p, q-p, s-r, s-r)
                d2[:,p:q,p:q,r:s,r:s] = d2_s_iijj
                d2[:,r:s,r:s,p:q,p:q] = d2_s_iijj.transpose (0,3,4,1,2)
                d2[(0,3),p:q,r:s,r:s,p:q] = -d2_s_iijj[(0,3),...].transpose (0,1,4,3,2)
                d2[(0,3),r:s,p:q,p:q,r:s] = -d2_s_iijj[(0,3),...].transpose (0,3,2,1,4)

    def _crunch_1e (bra, ket, s1)
        d1 = tdm1s[bra,ket]
        d2 = tdm1s[bra,ket]
        frag_hop_list = hopping_index[:,bra,ket]
        assert (np.count_nonzero (frag_hop_list) == 2)
        assert (frag_hop_list.sum () == 0)
        assert (np.amax (frag_hop_list) == 1)
        i = np.where (frag_hop_list == 1)[0][0]
        j = np.where (frag_hop_list == -1)[0][0]
        spect_frags = np.where (frag_hop_list == 0)[0]
        inti, intj = ints[i], ints[j]
        p, r = sum (nlas[:i]), sum (nlas[:j])
        q, s = p + nlas[i], r + nlas[j]
        d1[s1,:,:] = np.multiply.outer (ints[i].get_p (bra, ket, s1), ints[j].get_h (bra, ket, s1))
        s1a = s1 * 2  # aa: 0, ba: 2
        s1b = s1a + 2 # ab: 1, bb: 3 (range specifier: I want [s1a, s1a + 1], which requires s1a:s1a+2 because of how Python ranges work)
        s1s1 = s1 * 3 # aa: 0, bb: 3
        def _crunch_1e_tdm2 (d2_ijkk, i0, i1, j0, j1, k0, k1):
            d2[s1a:s1b, i0:i1, j0:j1, k0:k1, k0:k1] = d2_ijkk
            d2[s1a:s1b ,k0:k1, k0:k1, i0:i1, j0:j1] = d2_ijkk.transpose (0,3,4,1,2)
            d2[s1s1, i0:i1, k0:k1, k0:k1, j0:j1] = -d2_ijkk[s1,...].transpose (0,3,2,1)
            d2[s1s1, k0:k1, j0:j1, i0:i1, k0:k1] = -d2_ijkk[s1,...].transpose (2,1,0,3)
        # pph (transpose is from Dirac order to Mulliken order)
        d2_ijii = np.multiply.outer (ints[i].get_pph (bra, ket, s1), ints[j].get_h (bra, ket, s1)).transpose (0,1,4,2,3)
        _crunch_1e_tdm2 (d2_ijii, p, q, r, s, p, q)
        # phh (transpose is to bring spin onto the outside and then from Dirac order to Mulliken order)
        d2_ijjj = np.multiply.outer (ints[i].get_p (bra, ket, s1), ints[j].get_phh (bra, ket, s1)).transpose (1,0,4,2,3)
        _crunch_1e_tdm2 (d2_ijjj, p, q, r, s, r, s)
        # spectator fragment mean-field (should automatically be in Mulliken order)
        for k in np.where (frag_hop_list == 0)[0]:
            t = sum (nlas[:k])
            u = t + nlas[k]
            d1_skk = ints[k].get_dm1 (bra, ket)
            d2_ijkk = np.multiply.outer (d1, ints[k].get_dm1 (bra, ket)).transpose (2,0,1,3,4)
            _crunch_1e_tdm2 (d2_ijkk, p, q, r, s, t, u)

    def _crunch_spin_hop (bra, ket):
        d2 = tdm2s[bra, ket] # aa, ab, ba, bb -> 0, 1, 2, 3
        i = np.where (np.all (hopping_index[:,:,bra,ket] == [1,-1]))[0][0]
        j = np.where (np.all (hopping_index[:,:,bra,ket] == [-1,1]))[0][0]
        p, r = sum (nlas[:i]), sum (nlas[:j])
        q, s = p + nlas[i], r + nlas[j]
        d2_spsm = np.multiply.outer (ints[i].get_sp (bra, ket), ints[j].get_sm (bra, ket))
        d2[1,p:q,r:s,r:s,p:q] = -d2_spsm.transpose (0,3,2,1)
        d2[2,r:s,p:q,p:q,r:s] = -d2_spsm.transpose (2,1,0,3)

    def _crunch_pair_hop (bra, ket, s2lt):
        # s2lt: 0, 1, 2 -> aa, ab, bb
        # s2: 0, 1, 2, 3 -> aa, ab, ba, bb
        s2 = (0, 1, 3)[s2lt]
        d2 = tdm2s[bra, ket]
        i = np.where (hopping_index.sum (1)[:,bra,ket] ==  2)[0][0]
        j = np.where (hopping_index.sum (1)[:,bra,ket] == -2)[0][0]
        p, r = sum (nlas[:i]), sum (nlas[:j])
        q, s = p + nlas[i], r + nlas[j]
        pp = ints[i].get_pp (bra, ket, s2lt)
        if s2lt == 1: assert (np.all (np.abs (pp + pp.T)) < 1e-8), '{}'.format (np.amax (np.abs (pp + pp.T)))
        hh = ints[j].get_hh (bra, ket, s2lt)
        if s2lt == 1: assert (np.all (np.abs (hh + hh.T)) < 1e-8), '{}'.format (np.amax (np.abs (hh + hh.T)))
        d2_ijij = np.multiply.outer (pp, hh).transpose (0,3,1,2) # Dirac -> Mulliken order
        d2[s2,p:q,r:s,p:q,r:s] = d2_ijij
        if s2lt == 1: # ab -> ba
            d2[2,p:q,r:s,p:q,r:s] = d2_ijij.transpose (2,3,0,1)
        # Electron 1 and electron 2 have the same ranges -> e- perm redundant for aa, bb
        # "Exchange" should be built into the same-spin pp and hh intermediates (see asserts above)

    def _crunch_pair_split (bra, ket, s2lt):
        # s2lt: 0, 1, 2 -> aa, ab, bb
        # s2: 0, 1, 2, 3 -> aa, ab, ba, bb
        s2  = (0, 1, 3)[s2lt] # aa, ab, bb
        s2T = (0, 2, 3)[s2lt] # aa, ba, bb -> when you populate the e1 <-> e2 permutation
        d2 = tdm2s[bra, ket]
        if s2lt == 1:
            s1i, s1j = 0, 1
            i = np.where (hopping_index[:,0,bra,ket] == 1)[0][0]
            j = np.where (hopping_index[:,1,bra,ket] == 1)[0][0]
        else:
            i, j = np.where (hopping_index.sum (1)[:,bra,ket] == 1)[0]
            s1i = s1j = s2lt // 2
        k = np.where (hopping_index.sum (1)[:,bra,ket] == -2)[0][0]
        pp = np.multiply.outer (ints[i].get_p (bra, ket, s1i), ints[j].get_p (bra, ket, s1j))
        hh = ints[k].get_hh (bra, ket, s2lt)
        if s2lt == 1: assert (np.all (np.abs (hh + hh.T)) < 1e-8), '{}'.format (np.amax (np.abs (hh + hh.T)))
        d2_ikjk = np.multiply.outer (pp, hh).transpose (0,3,1,2) # Dirac -> Mulliken transpose
        p, r, t = sum (nlas[:i]), sum (nlas[:j]), sum (nlas[:k])
        q, s, u = p + nlas[i], r + nlas[j], t + nlas[k]
        d2[s2, p:q,t:u,r:s,t:u] = d2_ikjk
        d2[s2T,r:s,t:u,p:q,t:u] = d2_ikjk.transpose (2,3,0,1)
        if s2 == s2T: # same-spin only: exchange happens, but should be built into hh
            test = d2[s2,p:q,t:u,r:s,t:u] + d2[s2,r:s,t:u,p:q,t:u].transpose (2,3,0,1)
            assert (np.all (np.abs (test)) < 1e-8), '{}'.format (np.amax (np.abs (test)))

    def _crunch_2e (bra, ket, s2lt):
        # s2lt: 0, 1, 2 -> aa, ab, bb
        # s2: 0, 1, 2, 3 -> aa, ab, ba, bb
        s2  = (0, 1, 3)[s2lt] # aa, ab, bb
        s2T = (0, 2, 3)[s2lt] # aa, ba, bb -> when you populate the e1 <-> e2 permutation
        d2 = tdm2s[bra, ket]
        if s2lt == 1:
            s11, s12 = 0, 1
            i = np.where (hopping_index[:,0,bra,ket] ==  1)[0][0]
            k = np.where (hopping_index[:,1,bra,ket] ==  1)[0][0]
            j = np.where (hopping_index[:,0,bra,ket] == -1)[0][0]
            l = np.where (hopping_index[:,1,bra,ket] == -1)[0][0]
        else:
            s11 = s12 = s2lt // 2
            i, k = np.where (hopping_index.sum (1)[:,bra,ket] ==  1)[0]
            j, l = np.where (hopping_index.sum (1)[:,bra,ket] == -1)[0]
        pp = np.multiply.outer (ints[i].get_p (bra, ket, s11), ints[k].get_p (bra, ket, s12))
        hh = np.multiply.outer (ints[l].get_h (bra, ket, s11), ints[j].get_p (bra, ket, s12))
        d2_ijkl = np.multiply.outer (pp, hh).transpose (0,3,1,2) # Dirac -> Mulliken transpose
        p, r, t, v = sum (nlas[:i]), sum (nlas[:j]), sum (nlas[:k]), sum (nlas[:l])
        q, s, u, w = p + nlas[i], r + nlas[j], t + nlas[k], v + nlas[l]
        d2[s2, p:q,r:s,t:u,v:w] = d2_ijkl
        d2[s2T,r:s,t:u,p:q,t:u] = d2_ikjk.transpose (2,3,0,1)
        if s2 == s2T: # same-spin only: exchange happens
            d2[s2,p:q,v:w,t:u,r:s] = -d2_ijkl.transpose (0,3,2,1)
            d2[s2,t:u,r:s,p:q,v:w] = -d2_ijkl.transpose (2,1,0,3)

    # Second pass: upper-triangle
    t0 = (time.clock (), time.time ())
    for bra, ket in combinations (range (nroots), 2):
        spin = nspin_index[bra,ket]
        if not conserv_index[bra,ket]:
            continue
        elif nop_index[bra,ket] == 0:
            _crunch_null (bra, ket)
        elif nop_index[bra,ket] == 2:
            _crunch_1e (bra, ket, spin)
        elif nop_idnex[bra,ket] == 4:
            if ncharge_index[bra,ket] == 0:
                assert (spin == 1)
                _crunch_spin_hop (bra, ket)
            elif nfrag_index[bra,ket] == 2:
                _crunch_pair_hop (bra, ket, spin)
            elif nfrag_index[bra,ket] == 3:
                if np.amin (hopping_index.sum (1)[:,bra,ket]) == -2:
                    _crunch_pair_split (bra, ket, spin)
                else:
                    _crunch_pair_split (ket, bra, spin)
            elif nfrag_index[bra,ket] == 4:
                _crunch_2e (bra, ket, spin)
    t0 = lib.logger.timer (las, 'LAS-state TDM12s upper-triangle outer-producting', *t0)

    # Third pass: + adjoint and diagonal
    tdm1s += tdm1s.conj ().transpose (1,0,2,4,3)
    tdm2s += tdm1s.conj ().transpose (1,0,2,4,3,6,5)
    for ket in range (nroots):
        _crunch_null (ket, ket)
    t0 = lib.logger.timer (las, 'LAS-state TDM12s + adjoint and diagonal outer-producting', *t0)

    return tdm1s, tdm2s

