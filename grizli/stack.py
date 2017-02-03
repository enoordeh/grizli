"""
Utilities for fitting stacked spectra
"""
from collections import OrderedDict
from imp import reload

import astropy.io.fits as pyfits
import numpy as np
    
def make_templates(grism='G141', return_lists=False):
    """Generate template savefile
    
    This script generates the template sets with the emission line 
    complexes and with individual lines.
    
    Parameters
    ----------
    grism : str
        Grism of interest, which defines what FWHM to use for the line
        templates.
    
    return_lists : bool
        Return the templates rather than saving them to a file
        
    Returns
    -------
    t_complexes, t_lines : list
        If `return` then return two lists of templates.  Otherwise, 
        store them to a `~numpy` save file "templates_{fwhm}.npy".
        
    """
    
    from grizli.multifit import Multibeam
    
    if grism == 'G141':    # WFC3/IR
        fwhm = 1100
    elif grism == 'G800L': # ACS/UVIS
        fwhm = 1400
    elif grism == 'G280':  # WFC3/UVIS
        fwhm = 1500
    elif grism == 'GRISM': # WFIRST
        fwhm = 350
    else:
        fwhm = 700 # G102
        
    # Line complex templates
    t_complexes = MultiBeam.load_templates(fwhm=fwhm, line_complexes=True)
    
    # Individual lines
    line_list = ['SIII', 'SII', 'Ha', 'OI-6302', 'OIII', 'Hb', 
                 'OIII-4363', 'Hg', 'Hd', 'NeIII', 'OII', 'MgII']
                 
    t_lines = MultiBeam.load_templates(fwhm=fwhm, line_complexes=False,
                                       full_line_list=line_list)
    
    if return_lists:
        return t_complexes, t_lines
    else:
        # Save them to a file
        np.save('templates_{0}.npy'.format(fwhm), [t_complexes, t_lines])
        print('Wrote `templates_{0}.npy`'.format(fwhm))

class StackFitter(object):
    def __init__(self, file='gnt_18197.stack.fits', sys_err=0.02, mask_min=0.1, fit_stacks=True, fcontam=1):
        """Object for fitting stacked spectra.
        
        Parameters
        ----------
        file : str
            Stack FITS filename.
        
        sys_err : float
            Minimum systematic error, interpreted as a fractional error.  
            The adjusted variance is taken to be
            
                >>> var = var0 + (sys_err*flux)**2
                
        mask_min : float
            Only fit 2D pixels where the flat-flambda model has pixel values
            greater than `mask_min` times the maximum of the model.
        
        fit_stacks : bool
            Fit the stacks of each grism combined from all available PAs.  If
            False, then fit the PAs individually.
        
        fcontam : float
            Parameter to control weighting of contaminated pixels for 
            `fit_stacks=False`.  
            
        """
        self.file = file
        self.hdulist = pyfits.open(file)
        
        self.h0 = self.hdulist[0].header.copy()
        self.Ngrism = self.h0['NGRISM']
        
        self.ext = []
        for i in range(self.Ngrism):
            g = self.h0['GRISM{0:03d}'.format(i+1)]
            if fit_stacks:
                self.ext.append(g)
            else:
                ng = self.h0['N{0}'.format(g)]
                for j in range(ng):
                    pa = self.h0['{0}{1:02d}'.format(g, j+1)]
                    self.ext.append('{0},{1}'.format(g,pa))
                
        self.Next = len(self.ext)
        self.E = []
        for i in range(self.Next):
            E_i = StackedSpectrum(file=self.file, sys_err=sys_err,
                                  mask_min=mask_min, extver=self.ext[i], 
                                  mask_threshold=-1, fcontam=fcontam)
            E_i.compute_model()
            
            self.E.append(E_i)
            
        self.Ndata = np.sum([E.size for E in self.E])
        self.scif = np.hstack([E.scif for E in self.E])
        self.ivarf = np.hstack([E.ivarf for E in self.E])

        self.weightf = np.hstack([E.weightf for E in self.E])
        self.ivarf *= self.weightf

        self.sivarf = np.sqrt(self.ivarf)

        self.fit_mask = np.hstack([E.fit_mask for E in self.E])
        self.DoF = int((self.fit_mask*self.weightf).sum())
        
        self.slices = self._get_slices()
        
        self.Abg = self._init_background()
        
    def _get_slices(self):
        """Precompute array slices for how the individual components map into the single combined arrays.
        
        Parameters
        ----------
        None 
        
        Returns
        -------
        slices : list
            List of slices.
        """
        x = 0
        slices = []
        for i in range(self.Next):
            slices.append(slice(x+0, x+self.E[i].size))
            x += self.E[i].size
        
        return slices
        
    def _init_background(self):
        """Initialize the (flat) background model components
        
        Parameters
        ----------
        None :
        
        Returns
        -------
        Abg : `~np.ndarray`
            
        """
        Abg = np.zeros((self.Next, self.Ndata))
        for i in range(self.Next):
            Abg[i, self.slices[i]] = 1.
        
        return Abg
    
    def compute_model(self, spectrum_1d=None):
        """
        TBD
        """
        return False
        
    def fit_at_z(self, z=0, templates=[], fitter='nnls', get_uncertainties=False):
        """Fit the 2D spectra with a set of templates at a specified redshift.
        
        Parameters
        ----------
        z : float
            Redshift.
        
        templates : list
            List of templates to fit.
        
        fitter : str
            Minimization algorithm to compute template coefficients.
            The default 'nnls' uses non-negative least squares.  
            The other option is standard 'leastsq'.
        
        get_uncertainties : bool
            Compute coefficient uncertainties from the covariance matrix
        
        
        Returns
        -------
        chi2 : float
            Chi-squared of the fit
        
        background : `~np.ndarray`
            Background model
        
        full_model : `~np.ndarray`
            Best-fit 2D model.
        
        coeffs, err : `~np.ndarray`
            Template coefficients and uncertainties.
        
        """
        import scipy.optimize
        
        NTEMP = len(templates)
        A = np.zeros((self.Next+NTEMP, self.Ndata))
        A[:self.Next,:] += self.Abg
        
        for i, t in enumerate(templates):
            ti = templates[t]
            s = [ti.wave*(1+z), ti.flux/(1+z)]
            
            for j, E in enumerate(self.E):
                if (s[0][0] > E.wave.max()) | (s[0][-1] < E.wave.min()):
                    continue

                sl = self.slices[j]
                A[self.Next+i, sl] = E.compute_model(spectrum_1d=s)
                    
        oktemp = (A*self.fit_mask).sum(axis=1) != 0
        
        Ax = A[oktemp,:]*self.sivarf
        
        pedestal = 0.04
        
        AxT = Ax[:,self.fit_mask].T
        data = ((self.scif+pedestal)*self.sivarf)[self.fit_mask]
        
        if fitter == 'nnls':
            coeffs, rnorm = scipy.optimize.nnls(AxT, data)            
        else:
            coeffs, residuals, rank, s = np.linalg.lstsq(AxT, data)
                
        background = np.dot(coeffs[:self.Next], A[:self.Next,:]) - pedestal
        full = np.dot(coeffs[self.Next:], Ax[self.Next:,:]/self.sivarf)
        
        resid = self.scif - full - background
        chi2 = np.sum(resid[self.fit_mask]**2*self.ivarf[self.fit_mask])
        
        # Uncertainties from covariance matrix
        if get_uncertainties:
            try:
                covar = np.matrix(np.dot(AxT.T, AxT)).I
                covard = np.sqrt(covar.diagonal()).A.flatten()
            except:
                print('Except!')
                covard = np.zeros(oktemp.sum())#-1.
        else:
            covard = np.zeros(oktemp.sum())#-1.
        
        full_coeffs = np.zeros(NTEMP)
        full_coeffs[oktemp[self.Next:]] = coeffs[self.Next:]

        full_coeffs_err = np.zeros(NTEMP)
        full_coeffs_err[oktemp[self.Next:]] = covard[self.Next:]
        
        return chi2, background, full, full_coeffs, full_coeffs_err
    
    def fit_zgrid(self, dz0=0.005, zr=[0.4, 3.4], fitter='nnls', make_plot=True, save_data=True, prior=None, templates_file='templates.npy', verbose=True):
        """Fit templates on a redshift grid.
        
        Parameters
        ----------
        dz0 : float
            Initial step size of the redshift grid (dz/1+z).
        
        zr : list
            Redshift range to consider.
        
        fitter : str
            Minimization algorithm.  Default is non-negative least-squares.
        
        make_plot : bool
            Make the diagnostic plot.
        
        prior : list
            Naive prior to add to the nominal chi-squared(z) of the template
            fits.  The example below is a simple Gaussian prior centered
            at z=1.5. 
            
                >>> z_prior = np.arange(0,3,0.001)
                >>> chi_prior = (z_prior-1.5)**2/2/0.1**2
                >>> prior = [z_prior, chi_prior]
        
        templates_file : str
            Filename of the `~numpy` save file containing the templates.  Use 
            the `make_templates` script to generate this.
            
        verbose : bool
            Print the redshift grid steps.
        
        Returns
        -------
        hdu : `~astropy.io.fits.HDUList`
            Multi-extension FITS file with the result of the redshift fits.
        
        """
        import os
        import grizli
        import matplotlib.gridspec
        import matplotlib.pyplot as plt
        import numpy as np
        
        t_complex, t_i = np.load(templates_file)
        
        z = grizli.utils.log_zgrid(zr=zr, dz=dz0)
        chi2 = z*0.
        for i in range(len(z)):
            out = self.fit_at_z(z=z[i], templates=t_complex)
            chi2[i], bg, full, coeffs, err = out
            if verbose:
                print('{0:.4f} - {1:10.1f}'.format(z[i], chi2[i]))
        
        # Zoom in on the chi-sq minimum.
        ci = chi2
        zi = z
        for iter in range(1,7):
            if prior is not None:
                pz = np.interp(zi, prior[0], prior[1])
                cp = ci+pz
            else:
                cp = ci
                
            iz = np.argmin(cp)
            z0 = zi[iz]
            dz = dz0/2.02**iter
            zi = grizli.utils.log_zgrid(zr=[z0-dz*4, z0+dz*4], dz=dz)
            ci = zi*0.
            for i in range(len(zi)):
                
                out = self.fit_at_z(z=zi[i], templates=t_complex,
                                    fitter=fitter)
                
                ci[i], bg, full, coeffs, err = out
                
                if verbose:
                    print('{0:.4f} - {1:10.1f}'.format(zi[i], ci[i]))
            
            z = np.append(z, zi)
            chi2 = np.append(chi2, ci)
        
        so = np.argsort(z)
        z = z[so]
        chi2 = chi2[so]
        
        # Apply the prior
        if prior is not None:
            pz = np.interp(z, prior[0], prior[1])
            chi2 += pz
        
        # Get the fit with the individual line templates at the best redshift
        chi2x, bgz, fullz, coeffs, err = self.fit_at_z(z=z[np.argmin(chi2)], templates=t_i, fitter=fitter, get_uncertainties=True)
        
        # Table with z, chi-squared
        t = grizli.utils.GTable()
        t['z'] = z
        t['chi2'] = chi2
        
        if prior is not None:
            t['prior'] = pz
            
        # "area" parameter for redshift quality.
        num = np.trapz(np.clip(chi2-chi2.min(), 0, 25), z)
        denom = np.trapz(z*0+25, z)
        area25 = 1-num/denom
        
        # "best" redshift
        zbest = z[np.argmin(chi2)]
        
        # Metadata will be stored as header keywords in the FITS table
        t.meta = OrderedDict()
        t.meta['ID'] = (self.h0['ID'], 'Object ID')
        t.meta['RA'] = (self.h0['RA'], 'Right Ascension')
        t.meta['DEC'] = (self.h0['DEC'], 'Declination')
        t.meta['Z'] = (zbest, 'Best-fit redshift')
        t.meta['CHIMIN'] = (chi2.min(), 'Min Chi2')
        t.meta['CHIMAX'] = (chi2.max(), 'Min Chi2')
        t.meta['DOF'] = (self.DoF, 'Degrees of freedom')
        t.meta['AREA25'] = (area25, 'Area under CHIMIN+25')
        t.meta['FITTER'] = (fitter, 'Minimization algorithm')
        t.meta['HASPRIOR'] = (prior is not None, 'Was prior specified?')
        
        # Best-fit templates
        for i, te in enumerate(t_i):
            if i == 0:
                tc = t_i[te].zscale(0, scalar=coeffs[i])
                tl = t_i[te].zscale(0, scalar=coeffs[i])
            else:
                if te.startswith('line'):
                    tc += t_i[te].zscale(0, scalar=0.)
                else:
                    tc += t_i[te].zscale(0, scalar=coeffs[i])
                   
                tl += t_i[te].zscale(0, scalar=coeffs[i])
            
        # Get line fluxes, uncertainties and EWs
        il = 0
        for i, te in enumerate(t_i):
            if te.startswith('line'):
                il+=1
                
                if coeffs[i] == 0:
                    EW = 0.
                else:
                    tn = (t_i[te].zscale(0, scalar=coeffs[i]) +
                              tc.zscale(0, scalar=1))
                              
                    td = (t_i[te].zscale(0, scalar=0) + 
                             tc.zscale(0, scalar=1))
                             
                    clip = (td.wave <= t_i[te].wave.max())
                    clip &= (td.wave >= t_i[te].wave.min())
                    
                    EW = np.trapz((tn.flux/td.flux)[clip]-1, td.wave[clip])
                    if not np.isfinite(EW):
                        EW = -1000.
                        
                t.meta['LINE{0:03d}F'.format(il)] = (coeffs[i], 
                                       '{0} line flux'.format(te[5:]))
                
                t.meta['LINE{0:03d}E'.format(il)] = (err[i], 
                            '{0} line flux uncertainty'.format(te[5:]))
                
                #print('xxx EW', EW)
                t.meta['LINE{0:03d}W'.format(il)] = (EW, 
                            '{0} line rest EQW'.format(te[5:]))
                
        tfile = self.file.replace('.fits', '.zfit.full.fits')
        if os.path.exists(tfile):
            os.remove(tfile)

        t.write(tfile)
        
        ### Add image HDU and templates
        hdu = pyfits.open(tfile, mode='update') 
        hdu[1].header['EXTNAME'] = 'ZFIT'
        
        # oned_templates = np.array([tc.wave*(1+zbest), tc.flux/(1+zbest),
        #                            tl.flux/(1+zbest)])
        header = pyfits.Header()
        header['TEMPFILE'] = (templates_file, 'File with stored templates')
        hdu.append(pyfits.ImageHDU(data=coeffs, name='COEFFS'))
        
        for i in range(self.Next):
            E = self.E[i]
            model_i = fullz[self.slices[i]].reshape(E.sh)
            bg_i = bgz[self.slices[i]].reshape(E.sh)
            
            hdu.append(pyfits.ImageHDU(data=model_i, header=E.header,
                                       name='MODEL'))
        
            hdu.append(pyfits.ImageHDU(data=bg_i, header=E.header,
                                       name='BACKGROUND'))
        
        hdu.flush()
                                       
        if make_plot:
            self.make_fit_plot(hdu)

        return hdu
            
    def make_fit_plot(self, hdu):
        """Make a plot showing the fit
        
        Parameters
        ----------
        hdu : `~astropy.io.fits.HDUList`
            Fit results from `fit_zgrid`.
        
        Returns
        -------
        fig : `~matplotlib.figure.Figure`
            The figure object.

        """
        import matplotlib.pyplot as plt
        import matplotlib.gridspec
        import grizli
        
        zfit = grizli.utils.GTable.read(hdu[1])
        z = zfit['z']
        chi2 = zfit['chi2']
        
        # Initialize plot window
        height_ratios = [0.25]*self.Next
        height_ratios.append(1)
        gs = matplotlib.gridspec.GridSpec(self.Next+1,2, 
                                 width_ratios=[1,1.5+0.5*(self.Ngrism == 2)],
                                               height_ratios=height_ratios,
                                               hspace=0.)
                
        fig = plt.figure(figsize=[8+4*(self.Ngrism == 2), 3.5+0.5*self.Next])
        
        # Chi-squared
        axz = fig.add_subplot(gs[-1,0]) #121)
        
        axz.text(0.5, 1.02, self.file + '\n'+'z={0:.4f}'.format(z[np.argmin(chi2)]), ha='center', va='bottom', transform=axz.transAxes)
        
        axz.plot(z, chi2-chi2.min(), color='k')
        axz.fill_between(z, chi2-chi2.min(), 25, color='k', alpha=0.5)
        axz.set_ylim(0,25)
        axz.set_xlabel(r'$z$')
        axz.set_ylabel(r'$\chi^2$ - {0:.0f} ($\nu$={1:d})'.format(chi2.min(), self.DoF))
        axz.set_yticks([1,4,9,16,25])
        
        axz.set_xlim(z.min(), z.max())
        axz.grid()
        
        # 2D spectra
        twod_axes = []
        for i in range(self.Next):
            ax_i = fig.add_subplot(gs[i,1])

            model = hdu['MODEL', self.ext[i]].data
            ymax = model[np.isfinite(model)].max()
            #print('MAX', ymax)
            
            cmap = 'viridis_r'
            cmap = 'cubehelix_r'
            
            clean = self.E[i].sci - hdu['BACKGROUND', self.ext[i]].data
            clean *= self.E[i].fit_mask.reshape(self.E[i].sh)
            
            w = self.E[i].wave/1.e4
            
            ax_i.imshow(clean, vmin=-0.02*ymax, vmax=1.1*ymax, origin='lower',
                        extent=[w[0], w[-1], 0., 1.], aspect='auto',
                        cmap=cmap)
                        
            ax_i.set_xticklabels([]); ax_i.set_yticklabels([])
            twod_axes.append(ax_i)
                    
        axc = fig.add_subplot(gs[-1,1]) #224)
        
        # 1D Spectra + fit
        ymin = 1.e30
        ymax = -1.e30
        wmin = 1.e30
        wmax = -1.e30
        
        for i in range(self.Next):
            
            E = self.E[i]
            
            clean = E.sci - hdu['BACKGROUND', self.ext[i]].data
            fl, er = E.optimal_extract(clean)            
            flm, erm = E.optimal_extract(hdu['MODEL', self.ext[i]].data)
            w = E.wave/1.e4
            
            # Do we need to convert to F-lambda units?
            if E.is_flambda:
                unit_corr = 1.
                clip = (er > 0) & np.isfinite(er) & np.isfinite(flm)
                clip[:10] = False
                clip[-10:] = False
                
                if clip.sum() == 0:
                    clip = (er > -1)
            
            else:
                unit_corr = 1./E.sens
                clip = (E.sens > 0.1*E.sens.max()) 
                clip &= (np.isfinite(flm)) & (er > 0)
                
            fl *= 100*unit_corr
            er *= 100*unit_corr
            flm *= 100*unit_corr
        
            axc.errorbar(w[clip], fl[clip], er[clip], color='k', alpha=0.3, marker='.', linestyle='None')
            #axc.fill_between(w[clip], (fl+er)[clip], (fl-er)[clip], color='k', alpha=0.2)
            axc.plot(w[clip], flm[clip], color='r', alpha=0.6, linewidth=2) 
              
            # Plot limits              
            ymax = np.maximum(ymax, (flm+er*0.)[clip].max())
            ymin = np.minimum(ymin, (flm-er*0.)[clip].min())
            
            wmax = np.maximum(wmax, w.max())
            wmin = np.minimum(wmin, w.min())
                    
        axc.set_xlabel(r'$\lambda$')
        axc.set_ylabel(r'$f_\lambda \times 10^{-19}$')
        
        axc.set_ylim(ymin-0.2*ymax, 1.2*ymax)
        axc.grid()
                
        for ax in [axc]: #[axa, axb, axc]:
            ax.set_xlim(wmin, wmax)
        
        for ax in twod_axes:
            ax.set_xlim(wmin, wmax)
            
        gs.tight_layout(fig, pad=0.05, h_pad=0.01)
        fig.savefig(self.file.replace('.fits', '.zfit.full.png'))        
        return fig
                
class StackedSpectrum(object):
    def __init__(self, file='gnt_18197.stack.G141.285.fits', sys_err=0.02, mask_min=0.1, extver='G141', mask_threshold=7, fcontam=1.):
        import grizli
        
        self.sys_err = sys_err
        self.mask_min = mask_min
        self.extver = extver
        self.mask_threshold=mask_threshold
        
        self.file = file
        self.hdulist = pyfits.open(file)
        
        self.h0 = self.hdulist[0].header.copy()
        self.header = self.hdulist['SCI',extver].header.copy()
        self.sh = (self.header['NAXIS2'], self.header['NAXIS1'])
        self.wave = self.get_wavelength_from_header(self.header)
        
        # Configuration file
        self.is_flambda = self.header['ISFLAM']
        self.conf_file = self.header['CONF']
        self.conf = grizli.grismconf.aXeConf(self.conf_file)
        self.conf.get_beams()
        
        self.sci = self.hdulist['SCI',extver].data*1.
        self.ivar0 = self.hdulist['WHT',extver].data*1
        self.size = self.sci.size
        
        self.scif = self.sci.flatten()
        self.ivarf0 = self.ivar0.flatten()
        
        self.ivarf = 1/(1/self.ivarf0 + (sys_err*self.scif)**2)
        self.ivar = self.ivarf.reshape(self.sh)
        
        self.sivarf = np.sqrt(self.ivar).flatten()
        
        self.fit_mask = (self.ivarf > 0) 
        self.fit_mask &= np.isfinite(self.scif) & np.isfinite(self.ivarf)
        
        # Contamination weighting
        self.fcontam = fcontam
        if ('CONTAM',extver) in self.hdulist:
            self.contam = np.abs(self.hdulist['CONTAM',extver].data*1.)
            self.weight = np.exp(-fcontam*self.contam*np.sqrt(self.ivar0))
            self.contamf = self.contam.flatten()
            self.weightf = self.weight.flatten()
        else:
            self.contam = self.sci*0.
            self.contamf = self.scif*0.
            
            self.weight = self.sci*0.+1
            self.weightf = self.scif*0.+1
        
        # Spatial kernel
        self.kernel = self.hdulist['KERNEL',extver].data*1
        self.kernel /= self.kernel.sum()
        
        self._build_model()
        
        self.flat = self.compute_model()
        
        if self.is_flambda:
            elec = self.flat*self.ivarf0/self.ivarf0.max()
        else:
            elec = self.flat
            
        self.fit_mask &= elec > mask_min*elec.max()
        
        self.DoF = self.fit_mask.sum() #(self.ivar > 0).sum()
        
        if mask_threshold > 0:
            self.drizzle_mask(mask_threshold=mask_threshold)
    
    @classmethod    
    def get_wavelength_from_header(self, h):
        """
        Generate wavelength array from WCS keywords
        """
        w = (np.arange(h['NAXIS1'])+1-h['CRPIX1'])*h['CD1_1'] + h['CRVAL1']
        return w
            
    def optimal_extract(self, data):
        """
        Optimally-weighted 1D extraction
        """
        flatf = self.flat.reshape(self.sh).sum(axis=0)
        prof = self.flat.reshape(self.sh)/flatf
        
        num = prof*data*self.ivar
        den = prof**2*self.ivar
        opt_flux = num.sum(axis=0)/den.sum(axis=0)
        opt_var = 1./den.sum(axis=0)
        
        opt_rms = np.sqrt(opt_var)
        clip = (opt_var == 0) | ~np.isfinite(opt_var)
        opt_rms[clip] = 0
        opt_flux[clip] = 0
        
        return opt_flux, opt_rms
        
    def _build_model(self):
        """
        Initiazize components for generating 2D model
        """
        import grizli.utils_c as u
        
        NY = self.sh[0]
        data = np.zeros((self.header['NAXIS1'], self.header['NAXIS2'], self.header['NAXIS1']))
                                            
        for j in range(NY//2):
            data[j,:,:j+NY//2] += self.kernel[:, -NY//2-j:]
        
        for j in range(self.sh[1]-NY//2, self.sh[1]):
            data[j,:,-NY//2+j:] += self.kernel[:, :self.sh[1]-j+NY//2]
        
        for j in range(NY//2, self.sh[1]-NY//2):
            #print(j)
            data[j,:,j-NY//2:j+NY//2] += self.kernel
                
        self.fit_data = data.reshape(self.sh[1],-1)
        
        if not self.is_flambda:
            sens = u.interp.interp_conserve_c(self.wave, 
                                    self.conf.sens['A']['WAVELENGTH'],
                                    self.conf.sens['A']['SENSITIVITY'])
            
            self.sens = sens*np.median(np.diff(self.wave))*1.e-17
            self.fit_data = (self.fit_data.T*self.sens).T
            
    def compute_model(self, spectrum_1d=None):
        """
        Generate the model spectrum
        """
        import grizli.utils_c as u
        if spectrum_1d is None:
            fl = self.wave*0+1
        else:
            fl = u.interp.interp_conserve_c(self.wave, spectrum_1d[0], spectrum_1d[1])
            
        model = np.dot(fl, self.fit_data)#.reshape(self.sh)
        #self.model = model
        return model
        
        